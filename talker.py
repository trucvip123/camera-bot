"""
talker.py - Gửi âm thanh qua loa camera EZVIZ/Hikvision.

Chiến lược (thử lần lượt):
  1. RTSP back-channel (ONVIF): DESCRIBE → SETUP(audio recvonly) → PLAY → RTP/AAC interleaved
  2. ISAPI TwoWayAudio HTTP PUT  (chỉ hoạt động khi cùng mạng LAN)

Nguồn âm thanh hỗ trợ:
  • Microphone (PyAudio)
  • File âm thanh (WAV / MP3 / AAC … — dùng FFmpeg decode)
"""

import hashlib
import os
import queue
import re
import select
import shutil
import socket
import struct
import subprocess
import threading
import time

import numpy as np
import pyaudio
import requests
from requests.auth import HTTPDigestAuth

# ───────────────────────────── constants ─────────────────────────────────────
SAMPLE_RATE   = 16000
CHANNELS      = 1
CHUNK_FRAMES  = 1024          # ~64 ms @16 kHz


# ───────────────────────────── FFmpeg finder ─────────────────────────────────
def _find_ffmpeg() -> str | None:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg:
        return ffmpeg
    base = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Microsoft",
                        "WinGet", "Packages")
    if os.path.isdir(base):
        for name in os.listdir(base):
            if name.lower().startswith("gyan.ffmpeg"):
                for root, _dirs, files in os.walk(os.path.join(base, name)):
                    if "ffmpeg.exe" in files:
                        return os.path.join(root, "ffmpeg.exe")
    return None


# ═════════════════════════════ RTSP client ═══════════════════════════════════

class _RTSPClient:
    """
    RTSP 1.0 client — Digest Auth (với qop=auth support), interleaved TCP.
    Headers được lưu dưới dạng lowercase key để tránh case-sensitivity bugs.
    """

    def __init__(self, host: str, port: int, user: str, password: str,
                 log_fn=None):
        self.host       = host
        self.port       = port
        self.user       = user
        self.password   = password
        self._log       = log_fn or (lambda m: None)
        self.sock: socket.socket | None = None
        self.cseq       = 0
        self.session_id = ""
        self._realm     = ""
        self._nonce     = ""
        self._qop       = ""   # "auth" nếu camera yêu cầu qop
        self._auth_type = ""   # "basic" hoặc "digest"

    def connect(self, timeout: float = 10.0):
        self.sock = socket.create_connection((self.host, self.port),
                                             timeout=timeout)
        self.sock.settimeout(timeout)

    def close(self):
        if self.sock:
            try:
                self.sock.close()
            except Exception:
                pass
            self.sock = None

    # ── Auth (Basic hoặc Digest) ─────────────────────────────────────────────

    def _make_auth(self, method: str, uri: str) -> str:
        """Tạo Authorization header — tự động chọn Basic hoặc Digest."""
        if not self._auth_type:
            return ""

        if self._auth_type == "basic":
            import base64
            token = base64.b64encode(
                f"{self.user}:{self.password}".encode()
            ).decode()
            return f"Basic {token}"

        # Digest
        if not self._realm:
            return ""
        ha1 = hashlib.md5(
            f"{self.user}:{self._realm}:{self.password}".encode()
        ).hexdigest()
        ha2 = hashlib.md5(f"{method}:{uri}".encode()).hexdigest()

        if self._qop == "auth":
            import os as _os
            cnonce = hashlib.md5(_os.urandom(8)).hexdigest()[:8]
            nc     = "00000001"
            resp   = hashlib.md5(
                f"{ha1}:{self._nonce}:{nc}:{cnonce}:{self._qop}:{ha2}".encode()
            ).hexdigest()
            return (
                f'Digest username="{self.user}", realm="{self._realm}", '
                f'nonce="{self._nonce}", uri="{uri}", qop={self._qop}, '
                f'nc={nc}, cnonce="{cnonce}", response="{resp}"'
            )
        else:
            resp = hashlib.md5(f"{ha1}:{self._nonce}:{ha2}".encode()).hexdigest()
            return (
                f'Digest username="{self.user}", realm="{self._realm}", '
                f'nonce="{self._nonce}", uri="{uri}", response="{resp}"'
            )

    # ── low-level send/recv ───────────────────────────────────────────────────

    def _send(self, method: str, uri: str,
              extra: dict | None = None) -> tuple[int, dict, bytes]:
        self.cseq += 1
        lines = [
            f"{method} {uri} RTSP/1.0",
            f"CSeq: {self.cseq}",
            "User-Agent: CameraBot/1.0",
        ]
        if self.session_id:
            lines.append(f"Session: {self.session_id}")
        auth = self._make_auth(method, uri)
        if auth:
            lines.append(f"Authorization: {auth}")
        for k, v in (extra or {}).items():
            lines.append(f"{k}: {v}")
        lines += ["", ""]
        self.sock.sendall("\r\n".join(lines).encode())
        return self._recv()

    def _recv(self) -> tuple[int, dict, bytes]:
        """
        Đọc RTSP response đầy đủ (header + body theo Content-Length).
        Headers được lưu lowercase để tránh case-sensitivity.
        Tự động extract realm/nonce/qop từ WWW-Authenticate.
        """
        raw = b""
        while b"\r\n\r\n" not in raw:
            chunk = self.sock.recv(4096)
            if not chunk:
                break
            raw += chunk

        if b"\r\n\r\n" not in raw:
            return 0, {}, raw

        hdr_raw, body_start = raw.split(b"\r\n\r\n", 1)
        hdr_str = hdr_raw.decode("utf-8", errors="ignore")
        lines   = hdr_str.split("\r\n")

        # Parse status code
        code = 0
        parts = lines[0].split() if lines else []
        if len(parts) >= 2:
            try:
                code = int(parts[1])
            except ValueError:
                pass

        # Parse headers — lowercase keys để tránh case bug
        headers: dict[str, str] = {}
        for line in lines[1:]:
            if ": " in line:
                k, v = line.split(": ", 1)
                headers[k.strip().lower()] = v.strip()

        # Drain full body (tránh body leftover làm corrupt request tiếp theo)
        cl = int(headers.get("content-length", "0"))
        body = body_start
        while len(body) < cl:
            chunk = self.sock.recv(4096)
            if not chunk:
                break
            body += chunk

        # Extract auth challenge từ 401
        if code == 401:
            www = headers.get("www-authenticate", "")
            self._log(f"  [auth] WWW-Authenticate: {www[:80]}")
            if www.lower().startswith("basic"):
                # Basic auth — chỉ cần realm, không cần nonce
                self._auth_type = "basic"
                m = re.search(r'realm="([^"]+)"', www, re.IGNORECASE)
                if m:
                    self._realm = m.group(1)
            else:
                # Digest auth
                self._auth_type = "digest"
                m = re.search(r'realm="([^"]+)"', www, re.IGNORECASE)
                if m:
                    self._realm = m.group(1)
                m = re.search(r'nonce="([^"]+)"', www, re.IGNORECASE)
                if m:
                    self._nonce = m.group(1)
                m = re.search(r'qop="?([^"\s,]+)"?', www, re.IGNORECASE)
                self._qop = m.group(1).lower() if m else ""
            self._log(f"  [auth] type={self._auth_type!r} realm={self._realm!r} qop={self._qop!r}")

        return code, headers, hdr_raw + b"\r\n\r\n" + body

    # ── RTSP verbs ────────────────────────────────────────────────────────────

    def describe(self, url: str,
                 extra: dict | None = None) -> tuple[int, dict, str]:
        """DESCRIBE với tự động retry sau 401."""
        _hdrs = {"Accept": "application/sdp"}
        if extra:
            _hdrs.update(extra)
        code, hdrs, data = self._send("DESCRIBE", url, _hdrs)
        if code == 401:
            # realm/nonce đã được set trong _recv, retry với auth
            code, hdrs, data = self._send("DESCRIBE", url, _hdrs)
        sdp = ""
        if code == 200 and b"\r\n\r\n" in data:
            body = data.split(b"\r\n\r\n", 1)[1]
            cl = int(hdrs.get("content-length", "0"))
            while len(body) < cl:
                body += self.sock.recv(4096)
            sdp = body.decode("utf-8", errors="ignore")
        return code, hdrs, sdp

    def setup(self, track_url: str, transport: str) -> tuple[int, dict]:
        code, hdrs, _ = self._send("SETUP", track_url,
                                   {"Transport": transport})
        if code == 401:
            code, hdrs, _ = self._send("SETUP", track_url,
                                       {"Transport": transport})
        if code == 200:
            sess = hdrs.get("session", "")  # lowercase key
            if sess:
                self.session_id = sess.split(";")[0].strip()
        return code, hdrs

    def play(self, url: str) -> int:
        code, _, _ = self._send("PLAY", url, {"Range": "npt=0.000-"})
        if code == 401:
            code, _, _ = self._send("PLAY", url, {"Range": "npt=0.000-"})
        return code

    def record(self, url: str) -> int:
        code, _, _ = self._send("RECORD", url, {"Range": "npt=0.000-"})
        if code == 401:
            code, _, _ = self._send("RECORD", url, {"Range": "npt=0.000-"})
        return code

    def teardown(self, url: str):
        try:
            self._send("TEARDOWN", url)
        except Exception:
            pass

    def send_rtp(self, channel: int, rtp_data: bytes):
        """Gửi RTP interleaved: $ + channel(1B) + length(2B) + data."""
        hdr = struct.pack("!BBH", 0x24, channel & 0xFF, len(rtp_data))
        try:
            self.sock.sendall(hdr + rtp_data)
        except OSError:
            pass


# ═════════════════════════════ RTP builder ═══════════════════════════════════

class _RTPBuilder:
    """RTP packet builder cho AAC-hbr (RFC 3640)."""
    PT = 104

    def __init__(self):
        import random
        self.ssrc      = random.randint(1, 0xFFFFFFFF)
        self.seq       = 0
        self.timestamp = 0

    def packet(self, aac_raw: bytes, mark: bool = True) -> bytes:
        self.seq = (self.seq + 1) & 0xFFFF
        rtp_hdr = struct.pack(
            "!BBHII",
            0x80,
            (0x80 if mark else 0) | (self.PT & 0x7F),
            self.seq,
            self.timestamp,
            self.ssrc,
        )
        self.timestamp = (self.timestamp + 1024) & 0xFFFFFFFF
        # RFC 3640: AU-headers-length(2B) + AU-header(2B): size[13]|index[3]
        au_section = struct.pack("!HH", 16, (len(aac_raw) << 3) & 0xFFFF)
        return rtp_hdr + au_section + aac_raw


# ═════════════════════════════ AAC encoder ═══════════════════════════════════

class _AACEncoder:
    """PCM s16le → raw AAC frames qua FFmpeg ADTS output."""

    def __init__(self, ffmpeg: str, sample_rate: int = 16000, channels: int = 1):
        self.ffmpeg      = ffmpeg
        self.sample_rate = sample_rate
        self.channels    = channels
        self._proc: subprocess.Popen | None = None
        self._q: queue.Queue[bytes] = queue.Queue(maxsize=64)
        self._running    = False

    def start(self):
        """Khởi động encoder PCM s16le → ADTS AAC (dùng cho mic)."""
        cmd = [
            self.ffmpeg, "-loglevel", "quiet",
            "-f", "s16le", "-ar", str(self.sample_rate),
            "-ac", str(self.channels), "-i", "pipe:0",
            "-c:a", "aac", "-profile:a", "aac_low",
            "-ar", str(self.sample_rate), "-ac", str(self.channels),
            "-b:a", "48k", "-frame_size", "1024", "-f", "adts", "pipe:1",
        ]
        self._proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        self._running = True
        threading.Thread(target=self._reader, daemon=True).start()

    def start_file(self, file_path: str):
        """File → ADTS AAC trực tiếp: 1 FFmpeg, không qua PCM trung gian."""
        cmd = [
            self.ffmpeg, "-loglevel", "quiet", "-re",
            "-i", file_path, "-vn",
            "-c:a", "aac", "-profile:a", "aac_low",
            "-ar", str(self.sample_rate), "-ac", str(self.channels),
            "-b:a", "48k", "-frame_size", "1024", "-f", "adts", "pipe:1",
        ]
        self._proc = subprocess.Popen(
            cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        self._running = True
        threading.Thread(target=self._reader, daemon=True).start()

    def feed(self, pcm: bytes):
        if self._proc and self._running:
            try:
                self._proc.stdin.write(pcm)
                self._proc.stdin.flush()
            except (BrokenPipeError, OSError):
                pass

    def get(self, timeout: float = 0.05) -> bytes | None:
        try:
            return self._q.get(timeout=timeout)
        except queue.Empty:
            return None

    def stop(self):
        self._running = False
        if self._proc:
            try:
                self._proc.stdin.close()
            except Exception:
                pass
            try:
                self._proc.wait(timeout=2)
            except Exception:
                self._proc.kill()

    def _reader(self):
        buf = b""
        while self._running:
            chunk = self._proc.stdout.read(4096)
            if not chunk:
                break
            buf += chunk
            while len(buf) >= 7:
                idx = -1
                for i in range(len(buf) - 1):
                    if buf[i] == 0xFF and (buf[i + 1] & 0xF0) == 0xF0:
                        idx = i
                        break
                if idx < 0:
                    buf = buf[-1:]
                    break
                if idx > 0:
                    buf = buf[idx:]
                if len(buf) < 7:
                    break
                frame_len = (
                    ((buf[3] & 0x03) << 11)
                    | (buf[4] << 3)
                    | ((buf[5] & 0xE0) >> 5)
                )
                if frame_len < 7:
                    buf = buf[1:]
                    continue
                if len(buf) < frame_len:
                    break
                adts_hdr = 9 if (buf[1] & 0x01) == 0 else 7
                raw = buf[adts_hdr:frame_len]
                if raw:
                    try:
                        self._q.put_nowait(raw)
                    except queue.Full:
                        pass
                buf = buf[frame_len:]
        self._running = False  # báo hiệu encoder đã xong


# ═════════════════════════════ G.711 µ-law ════════════════════════════════════
try:
    import audioop
    def _pcm16_to_ulaw(pcm: bytes) -> bytes:
        return audioop.lin2ulaw(pcm, 2)
except ImportError:
    def _pcm16_to_ulaw(pcm: bytes) -> bytes:
        MU = 255.0
        s = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
        n = np.clip(s / 32768.0, -1.0, 1.0)
        e = np.sign(n) * np.log1p(MU * np.abs(n)) / np.log1p(MU)
        return ((e + 1.0) * 127.5).clip(0, 255).astype(np.uint8).tobytes()


# ═════════════════════════════ AudioTalker ════════════════════════════════════

class AudioTalker:
    """
    Gửi âm thanh (mic hoặc file) qua loa camera EZVIZ.

    Dùng:
        t = AudioTalker(camera_ip=..., rtsp_port=554, ...)
        t.start()                   # mic
        t.start_file("song.mp3")    # file
        t.stop()
    """

    def __init__(
        self,
        camera_ip:  str,
        http_port:  int = 80,
        rtsp_port:  int = 554,
        username:   str = "admin",
        password:   str = "",
        rtsp_path:  str = "/Streaming/Channels/101",
        mic_index:  int | None = None,
        on_log=None,
        on_status=None,
    ):
        self.camera_ip = camera_ip
        self.http_port = http_port
        self.rtsp_port = rtsp_port
        self.username  = username
        self.password  = password
        self.rtsp_path = rtsp_path
        self.mic_index = mic_index
        self.on_log    = on_log
        self.on_status = on_status
        self.running   = False

    # ── public API ────────────────────────────────────────────────────────────

    def start(self):
        if self.running:
            return
        self.running = True
        threading.Thread(target=self._run_mic, daemon=True).start()

    def start_file(self, file_path: str):
        if self.running:
            return
        self.running = True
        threading.Thread(target=self._run_file, args=(file_path,),
                         daemon=True).start()

    def stop(self):
        self.running = False

    def close(self):
        self.stop()

    # ── mic source ────────────────────────────────────────────────────────────

    def _run_mic(self):
        ffmpeg = _find_ffmpeg()
        if not ffmpeg:
            self._log("❌ Không tìm thấy FFmpeg.")
            self._done()
            return

        pa = pyaudio.PyAudio()
        try:
            idx = self.mic_index
            mic_name = (pa.get_device_info_by_index(idx)["name"] if idx is not None
                        else pa.get_default_input_device_info()["name"])
        except Exception as e:
            self._log(f"❌ Không lấy được tên mic: {e}")
            pa.terminate()
            self._done()
            return
        pa.terminate()

        def _src(enc: _AACEncoder):
            enc.start()  # khởi động PCM→AAC encoder
            _pa = pyaudio.PyAudio()
            s = _pa.open(
                format=pyaudio.paInt16, channels=CHANNELS, rate=SAMPLE_RATE,
                input=True, input_device_index=self.mic_index,
                frames_per_buffer=CHUNK_FRAMES,
            )
            try:
                while self.running:
                    enc.feed(s.read(CHUNK_FRAMES, exception_on_overflow=False))
            finally:
                s.stop_stream(); s.close(); _pa.terminate()

        self._run_with_encoder(ffmpeg, _src, f"mic: {mic_name[:40]}")

    # ── file source ───────────────────────────────────────────────────────────

    def _run_file(self, file_path: str):
        ffmpeg = _find_ffmpeg()
        if not ffmpeg:
            self._log("❌ Không tìm thấy FFmpeg.")
            self._done()
            return
        if not os.path.isfile(file_path):
            self._log(f"❌ Không tìm thấy file: {file_path}")
            self._done()
            return

        label = os.path.basename(file_path)

        def _src(enc: _AACEncoder):
            # 1 FFmpeg pipeline: file → ADTS AAC trực tiếp, không qua PCM
            enc.start_file(file_path)

        self._run_with_encoder(ffmpeg, _src, label)

    # ── core engine ───────────────────────────────────────────────────────────

    def _run_with_encoder(self, ffmpeg: str, source_fn, label: str):
        """Thử RTSP back-channel → nếu thất bại thử ISAPI."""
        if self._try_rtsp_backchannel(ffmpeg, source_fn, label):
            return
        self._log("↩ Thử ISAPI TwoWayAudio (chỉ hoạt động trên LAN)…")
        self._try_isapi(source_fn)

    # ── Method 1: RTSP back-channel (ONVIF) ──────────────────────────────────

    def _try_rtsp_backchannel(self, ffmpeg: str, source_fn, label: str) -> bool:
        # Thử port chính trước, sau đó thử các port dự phòng nếu bị từ chối
        _FALLBACK_PORTS = [554, 8554, 10554]
        ports_to_try = [self.rtsp_port] + [
            p for p in _FALLBACK_PORTS if p != self.rtsp_port
        ]

        client = None
        used_port = self.rtsp_port
        for port in ports_to_try:
            _c = _RTSPClient(self.camera_ip, port,
                             self.username, self.password, self._log)
            try:
                _c.connect(timeout=5)
                client = _c
                used_port = port
                if port != self.rtsp_port:
                    self._log(f"  [rtsp] Kết nối thành công trên port {port}")
                break
            except ConnectionRefusedError:
                _c.close()
                if port != ports_to_try[-1]:
                    self._log(f"  [rtsp] Port {port} bị từ chối, thử {ports_to_try[ports_to_try.index(port)+1]}…")
                continue
            except OSError:
                _c.close()
                continue

        if client is None:
            self._log(
                f"⚠ RTSP: tất cả port {ports_to_try} đều bị từ chối.\n"
                f"  → Kiểm tra camera có bật RTSP không (Settings > Video > RTSP)\n"
                f"  → Thử dùng địa chỉ DDNS thay vì IP LAN nếu kết nối từ xa"
            )
            self._done()
            return False

        base_url = f"rtsp://{self.camera_ip}:{used_port}{self.rtsp_path}"
        try:
            # Require header kích hoạt back-channel trên camera EZVIZ/Hikvision
            code, _h, sdp = client.describe(
                base_url,
                {"Require": "www.onvif.org/ver20/backchannel"}
            )
            if code == 551:  # Option Not Supported — thử lại không có Require
                self._log("  [rtsp] Camera không hỗ trợ ONVIF backchannel, thử không Require…")
                code, _h, sdp = client.describe(base_url)
            if code != 200:
                self._log(f"⚠ RTSP DESCRIBE → {code}. Thử ISAPI…")
                return False

            audio_ctrl = _parse_audio_ctrl(sdp)
            if not audio_ctrl:
                self._log("⚠ Không có audio back-channel trong SDP.")
                return False

            code, setup_hdrs = client.setup(
                audio_ctrl, "RTP/AVP/TCP;unicast;interleaved=0-1;mode=record"
            )
            if code not in (200, 204):
                self._log(f"⚠ RTSP SETUP audio → {code}. Thử ISAPI…")
                return False

            channel = 0
            m = re.search(r"interleaved=(\d+)",
                          setup_hdrs.get("transport", ""))  # lowercase key
            if m:
                channel = int(m.group(1))

            # Hikvision/EZVIZ dùng PLAY kể cả khi SETUP có mode=record
            code = client.play(base_url)
            if code not in (200, 204):
                self._log(f"⚠ RTSP PLAY → {code}. Thử ISAPI…")
                return False

            self._log(
                f"✅ RTSP back-channel OK (ch={channel}). Đang phát: {label}"
            )
            self._set_status("connected")

            encoder = _AACEncoder(ffmpeg, SAMPLE_RATE, CHANNELS)
            # source_fn chịu trách nhiệm khởi động encoder (start/start_file)
            threading.Thread(target=source_fn, args=(encoder,),
                             daemon=True).start()

            # Drain thread: đọc và bỏ qua dữ liệu camera gửi về (RTCP, keepalive)
            # Dùng select() để không thay đổi blocking mode của socket
            def _drain():
                while self.running:
                    try:
                        ready, _, _ = select.select([client.sock], [], [], 0.1)
                        if ready:
                            data = client.sock.recv(4096)
                            if not data:
                                break
                    except Exception:
                        break

            threading.Thread(target=_drain, daemon=True).start()

            _FRAME_DUR = 1024 / SAMPLE_RATE  # 64 ms mỗi AAC frame
            rtp    = _RTPBuilder()
            t_next = time.monotonic()
            try:
                while self.running:
                    frame = encoder.get(timeout=0.1)
                    if frame:
                        now  = time.monotonic()
                        wait = t_next - now
                        if 0 < wait < _FRAME_DUR * 3:
                            time.sleep(wait)
                        client.send_rtp(channel, rtp.packet(frame))
                        # Tiến schedule; nếu trễ hơn 1 frame thì reset
                        t_next = max(t_next + _FRAME_DUR,
                                     time.monotonic() - _FRAME_DUR)
                    elif not encoder._running and encoder._q.empty():
                        # File phát xong, không còn frame nào → dừng
                        self.running = False
                        break
            except Exception as e:
                self._log(f"⚠ Lỗi gửi RTP: {e}")
            finally:
                encoder.stop()
                client.teardown(base_url)

            return True

        except (ConnectionRefusedError, OSError) as e:
            self._log(f"⚠ Lỗi kết nối RTSP ({self.camera_ip}:{used_port}): {e}")
            return False
        except Exception as e:
            self._log(f"⚠ RTSP back-channel lỗi: {e}")
            return False
        finally:
            if client:
                client.close()
            self._done()

    # ── Method 2: ISAPI (LAN only) ────────────────────────────────────────────

    def _try_isapi(self, source_fn):
        OPEN_XML = (
            "<TwoWayAudioChannel><id>1</id>"
            "<audioCompressionType>G.711ulaw</audioCompressionType>"
            "</TwoWayAudioChannel>"
        )

        # Thử port HTTP dự phòng nếu port chính bị từ chối
        _HTTP_PORTS = [self.http_port] + [
            p for p in (80, 8080, 8000) if p != self.http_port
        ]
        base = None
        sess = requests.Session()
        sess.auth = HTTPDigestAuth(self.username, self.password)

        r = None
        for http_port in _HTTP_PORTS:
            try:
                _base = f"http://{self.camera_ip}:{http_port}"
                r = sess.put(
                    f"{_base}/ISAPI/System/TwoWayAudio/channels/1/open",
                    data=OPEN_XML,
                    headers={"Content-Type": "application/xml"},
                    timeout=5,
                )
                base = _base          # kết nối thành công
                if http_port != self.http_port:
                    self._log(f"  [isapi] Kết nối trên port {http_port}")
                break
            except requests.exceptions.ConnectionError as e:
                err_str = str(e)
                if "10061" in err_str or "refused" in err_str.lower():
                    if http_port != _HTTP_PORTS[-1]:
                        next_p = _HTTP_PORTS[_HTTP_PORTS.index(http_port) + 1]
                        self._log(f"  [isapi] Port {http_port} từ chối, thử {next_p}…")
                    continue
                # Lỗi khác (timeout, network unreachable…)
                self._log(
                    f"❌ ISAPI lỗi kết nối ({self.camera_ip}:{http_port}): {e}\n"
                    f"  → Kiểm tra IP camera có đúng không (hiện dùng: {self.camera_ip})\n"
                    f"  → Nếu dùng IP LAN, hãy thử địa chỉ DDNS/tên miền thay thế"
                )
                self._done()
                return
            except Exception as e:
                self._log(f"❌ ISAPI lỗi kết nối: {e}")
                self._done()
                return

        if base is None:
            self._log(
                f"❌ ISAPI: không kết nối được {self.camera_ip} trên các port {_HTTP_PORTS}.\n"
                f"  → Kiểm tra lại IP camera (hiện dùng: {self.camera_ip})\n"
                f"  → Thử dùng địa chỉ DDNS thay vì IP LAN\n"
                f"  → Camera có thể dùng port khác (vào web camera để kiểm tra)"
            )
            self._done()
            return

        if r is None or r.status_code not in (200, 201):
            code = r.status_code if r is not None else "?"
            self._log(
                f"❌ ISAPI TwoWayAudio: HTTP {code}\n"
                "  → Chức năng Nói qua loa cần PC cùng mạng LAN với camera.\n"
                "  → Từ internet: cần đăng ký EZVIZ Open API tại open.ys7.com"
            )
            self._done()
            return

        self._log("✅ ISAPI OK (LAN). Đang gửi giọng nói…")
        self._set_status("connected")

        ISAPI_RATE  = 8000
        ISAPI_CHUNK = 320

        class _ISAPIEncoder:
            def __init__(self_):
                self_._q: queue.Queue[bytes] = queue.Queue(maxsize=128)

            def feed(self_, pcm16k: bytes):
                arr = np.frombuffer(pcm16k, dtype=np.int16)
                ulaw = _pcm16_to_ulaw(arr[::2].tobytes())
                for i in range(0, len(ulaw), ISAPI_CHUNK):
                    chunk = ulaw[i:i + ISAPI_CHUNK]
                    if chunk:
                        try:
                            self_._q.put_nowait(chunk)
                        except queue.Full:
                            pass

            def get(self_, timeout=0.05):
                try:
                    return self_._q.get(timeout=timeout)
                except queue.Empty:
                    return None

            def start(self_): pass
            def stop(self_): pass

        enc = _ISAPIEncoder()
        threading.Thread(target=source_fn, args=(enc,), daemon=True).start()

        def _gen():
            while self.running:
                chunk = enc.get(0.1)
                if chunk:
                    yield chunk

        try:
            sess.put(
                f"{base}/ISAPI/System/TwoWayAudio/channels/1/audioData",
                data=_gen(),
                headers={"Content-Type": "application/octet-stream"},
                timeout=None, stream=True,
            )
        except Exception as e:
            self._log(f"⚠ Lỗi ISAPI stream: {e}")
        finally:
            try:
                sess.put(
                    f"{base}/ISAPI/System/TwoWayAudio/channels/1/close",
                    timeout=3,
                )
            except Exception:
                pass
            self._done()
            self._log("Đã đóng kênh ISAPI.")

    # ── helpers ───────────────────────────────────────────────────────────────

    def _done(self):
        self.running = False
        self._set_status("disconnected")

    def _log(self, msg: str):
        if self.on_log:
            self.on_log(msg)

    def _set_status(self, status: str):
        if self.on_status:
            self.on_status(status)


# ─── standalone SDP helper ───────────────────────────────────────────────────

def _parse_audio_ctrl(sdp: str) -> str | None:
    """Trả về control URL của audio back-channel (recvonly) trong SDP.
    Ưu tiên track a=recvonly; fallback sang track audio bất kỳ nếu không có."""
    sections: list[tuple[str | None, str | None]] = []  # (direction, ctrl)
    in_audio = False
    cur_ctrl: str | None = None
    cur_dir:  str | None = None

    for raw in sdp.split("\n"):
        line = raw.strip()
        if line.startswith("m=audio"):
            if in_audio:
                sections.append((cur_dir, cur_ctrl))
            in_audio = True
            cur_ctrl = cur_dir = None
        elif line.startswith("m="):
            if in_audio:
                sections.append((cur_dir, cur_ctrl))
            in_audio = False
            cur_ctrl = cur_dir = None
        if in_audio:
            if line.startswith("a=control:"):
                cur_ctrl = line.split("a=control:", 1)[1].strip()
            elif line in ("a=recvonly", "a=sendonly", "a=sendrecv"):
                cur_dir = line[2:]  # 'recvonly' / 'sendonly' / 'sendrecv'
    if in_audio:
        sections.append((cur_dir, cur_ctrl))

    # Ưu tiên recvonly (back-channel thực sự)
    for direction, ctrl in sections:
        if direction == "recvonly" and ctrl:
            return ctrl
    # Fallback: bất kỳ audio track nào có control URL
    for _direction, ctrl in sections:
        if ctrl:
            return ctrl
    return None
