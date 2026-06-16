"""
bot.py - Bot tự động chào khách hàng tại cửa hàng.

Hoàn toàn MIỄN PHÍ - Không cần API key:
  Camera RTSP mic → FFmpeg PCM → VAD
  → Google Speech Recognition (miễn phí)
  → Rule-based Q&A Engine (tùy chỉnh)
  → Google TTS / gTTS (miễn phí)
  → loa camera

Yêu cầu: pip install SpeechRecognition gtts
"""

import asyncio
import os
import subprocess
import sys
import tempfile
import threading
import time
import wave
from datetime import datetime, timedelta
from typing import Callable, Optional

import numpy as np

# ────────────────────────────── constants ─────────────────────────────────────
SAMPLE_RATE     = 16_000
CHANNELS        = 1
CHUNK_FRAMES    = 1_024     # ~64 ms @16 kHz

SPEECH_MIN_MS   = 400
SILENCE_END_MS  = 1_500
MAX_RECORD_MS   = 30_000

DEFAULT_VAD_THRESHOLD = 500

# ──────────────────────── Q&A defaults ────────────────────────────────────
FALLBACK_RESPONSE = ""  # Không phản hồi khi không khớp từ khóa

DEFAULT_QA_TEXT = """\
# ═════════════════════════════════════════════════════════════════════
# Kịch bản Q&A — Chỉnh sửa theo cửa hàng của bạn
# Định dạng:  từ_khóa1|từ_khóa2|... : câu trả lời
# Dòng bắt đầu bằng # là ghi chú, sẽ được bỏ qua
# ═════════════════════════════════════════════════════════════════════

xin chào|chào|cô ơi|có ở nhà không|alo|bán đồ|có ai ở nhà không|liêm ơi|bé ư|bé ơi|ơi|bán đồ i|bán đồ đi|bán i|bán đi|bán đi chị ơi|bán đi chị ư| : Xin chào quý khách! Xin chờ bà chủ tôi một chút ạ.
tạm biệt|bye|goodbye|hẹn gặp : Cảm ơn quý khách đã ghé thăm! Hẹn gặp lại bạn lần sau. Chúc bạn một ngày vui vẻ!
cảm ơn|cám ơn|thanks|thành ơn : Không có gì ạ! Rất vui được phục vụ quý khách.
gíá|bao nhiêu|tiền|chi phí : Để biết giá sản phẩm cụ thể, nhân viên sẽ tư vấn cho bạn ngay ạ.
giờ mở cửa|mấy giờ|giờ làm việc|bao giờ mở : Cửa hàng mở từ 8 giờ sáng đến 10 giờ tối, từ Thứ Hai đến Chủ Nhật.
sản phẩm|mặt hàng|bán gì|có gì|hàng gì : Cửa hàng có nhiều sản phẩm đa dạng. Bạn đang tìm kiếm loại hàng gì?
nhân viên|hỗ trợ|gặp người : Tôi sẽ gọi nhân viên đến hỗ trợ bạn ngay ạ!
wifi|mạng|password wifi|mật khẩu wifi : Mật khẩu Wifi của Thanh Liêm là thanhliem45.
thanh toán|trả tiền|quẹt thẻ|chuyển khoản : Cửa hàng chấp nhận tiền mặt, thẻ ngân hàng và chuyển khoản.
gọi nhân viên|cần giúp|ại : Không sao ạ! Tôi sẽ gọi nhân viên đến ngay."""

# ─────────────────────── Rule-based responder ───────────────────────────────

def _parse_qa_text(text: str) -> list:
    """Parse Q&A config text.
    Format:  keyword1|keyword2 : response
    Lines starting with '#' are comments.
    """
    pairs = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            continue
        kw_part, _, resp = line.partition(":")
        keywords = [k.strip().lower() for k in kw_part.split("|") if k.strip()]
        resp = resp.strip()
        if keywords and resp:
            pairs.append((keywords, resp))
    return pairs


class RuleBasedResponder:
    """Khớp từ khóa và trả về câu trả lời đã định sẵn."""

    def __init__(self, qa_text: str = "", fallback: str = FALLBACK_RESPONSE):
        self.pairs   = _parse_qa_text(qa_text)
        self.fallback = fallback

    def respond(self, text: str) -> str:
        text_lower = text.lower().strip()
        for keywords, response in self.pairs:
            if any(kw in text_lower for kw in keywords):
                return response
        return self.fallback


# ────────────────────────────── FFmpeg finder ─────────────────────────────────
def _find_ffmpeg() -> Optional[str]:
    import shutil
    found = shutil.which("ffmpeg")
    if found:
        return found
    base = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Microsoft",
                        "WinGet", "Packages")
    if os.path.isdir(base):
        for name in os.listdir(base):
            if name.lower().startswith("gyan.ffmpeg"):
                for root, _dirs, files in os.walk(os.path.join(base, name)):
                    if "ffmpeg.exe" in files:
                        return os.path.join(root, "ffmpeg.exe")
    return None


# ════════════════════════════════ StoreBot ═════════════════════════════════════

class StoreBot:
    """
    Bot chào khách hàng tại cửa hàng.

    Lắng nghe mic camera → VAD → Google STT (miễn phí) →
    Rule-based Q&A → edge-tts → loa camera.

    Sử dụng:
        bot = StoreBot(camera_ip="...", ..., qa_text="...")
        bot.start()
        ...
        bot.stop()
    """

    def __init__(
        self,
        camera_ip:     str,
        rtsp_port:     int,
        http_port:     int,
        username:      str,
        password:      str,
        rtsp_path:     str,
        qa_text:       str = DEFAULT_QA_TEXT,
        vad_threshold: int = DEFAULT_VAD_THRESHOLD,
        on_log:         Optional[Callable[[str], None]] = None,
        on_status:      Optional[Callable[[str], None]] = None,
        on_transcript:  Optional[Callable[[str, str], None]] = None,
    ):
        self.camera_ip     = camera_ip
        self.rtsp_port     = rtsp_port
        self.http_port     = http_port
        self.username      = username
        self.password      = password
        self.rtsp_path     = rtsp_path
        self.vad_threshold = vad_threshold
        self.on_log        = on_log        or (lambda m: None)
        self.on_status     = on_status     or (lambda s: None)
        self.on_transcript = on_transcript or (lambda q, a: None)

        self._responder = RuleBasedResponder(qa_text)
        self.running    = False
        self._speaking  = threading.Event()   # set khi bot đang phát âm → ngăn echo
        self._proc: Optional[subprocess.Popen] = None
        self._max_retries = 10
        self._retry_delay = 2  # seconds, will exponentially backoff
        self._daily_reconnect_thread = None
        self._daily_reconnect_hour = 7

    # ── public API ────────────────────────────────────────────────────────────

    def start(self):
        if self.running:
            return
        self.running = True
        self._daily_reconnect_thread = threading.Thread(
            target=self._daily_reconnect_worker,
            daemon=True,
        )
        self._daily_reconnect_thread.start()
        threading.Thread(target=self._run, daemon=True).start()

    def stop(self):
        self.running = False
        self._speaking.clear()
        if self._proc:
            try:
                self._proc.terminate()
            except Exception:
                pass
        self.on_status("disconnected")

    # ── RTSP audio capture ────────────────────────────────────────────────────

    @property
    def _rtsp_url(self) -> str:
        path = self.rtsp_path
        if not path.startswith("/"):
            path = "/" + path
        return (
            f"rtsp://{self.username}:{self.password}"
            f"@{self.camera_ip}:{self.rtsp_port}{path}"
        )

    def _run(self):
        ffmpeg = _find_ffmpeg()
        if not ffmpeg:
            self.on_log("❌ Không tìm thấy FFmpeg.")
            self.running = False
            self.on_status("disconnected")
            return

        retry_count = 0
        retry_delay = self._retry_delay

        while self.running:
            try:
                self._attempt_connection(ffmpeg)
                # If connection succeeds, reset retry counter
                retry_count = 0
                retry_delay = self._retry_delay
            except Exception as e:
                if not self.running:
                    break

                retry_count += 1
                if retry_count > self._max_retries:
                    self.on_log(f"❌ Không thể kết nối sau {self._max_retries} lần thử. Dừng bot.")
                    self.running = False
                    break

                self.on_log(f"⚠ Lỗi kết nối (lần {retry_count}/{self._max_retries}): {e}")
                self.on_log(f"🔄 Sẽ thử kết nối lại trong {retry_delay} giây...")
                self.on_status("reconnecting")

                # Wait before retry with exponential backoff
                for _ in range(int(retry_delay)):
                    if not self.running:
                        return
                    time.sleep(1)

                # Exponential backoff: cap at 30 seconds
                retry_delay = min(retry_delay * 1.5, 30)

        self.on_status("disconnected")
        self.on_log("Bot đã dừng.")

    def _attempt_connection(self, ffmpeg: str):
        """Attempt one connection to RTSP stream."""
        cmd = [
            ffmpeg, "-loglevel", "error",
            "-rtsp_transport", "tcp",
            "-i", self._rtsp_url,
            "-vn",
            "-acodec", "pcm_s16le",
            "-ar", str(SAMPLE_RATE),
            "-ac", str(CHANNELS),
            "-f", "s16le",
            "pipe:1",
        ]

        kwargs: dict = {}
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

        try:
            self._proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                **kwargs,
            )
            self.on_log("✅ Bot kết nối camera thành công. Đang lắng nghe...")
            self.on_status("listening")
            self._vad_loop()
            # Stream ended unexpectedly, will trigger reconnection
            raise RuntimeError("RTSP stream closed unexpectedly")
        except Exception as e:
            # Collect FFmpeg stderr if available
            if self._proc and self._proc.stderr:
                try:
                    err = self._proc.stderr.read(4096).decode(errors="replace").strip()
                    if err and "Error number" in err:
                        self.on_log(f"⚠ FFmpeg: {err}")
                except Exception:
                    pass
            raise

    def _daily_reconnect_worker(self):
        """Force reconnect at configured hour every day."""
        while self.running:
            now = datetime.now()
            next_run = now.replace(
                hour=self._daily_reconnect_hour,
                minute=0,
                second=0,
                microsecond=0,
            )
            if now >= next_run:
                next_run += timedelta(days=1)

            wait_seconds = int((next_run - now).total_seconds())
            for _ in range(wait_seconds):
                if not self.running:
                    return
                time.sleep(1)

            if not self.running:
                return

            self.on_log("⏰ 07:00 - Tự động ngắt để kết nối lại RTSP...")
            if self._proc:
                try:
                    self._proc.terminate()
                except Exception:
                    pass

    # ── Voice Activity Detection ──────────────────────────────────────────────

    def _vad_loop(self):
        CHUNK_BYTES = CHUNK_FRAMES * 2          # int16 = 2 bytes/sample
        CHUNK_MS    = CHUNK_FRAMES * 1000 // SAMPLE_RATE   # 64 ms/chunk

        speech_frames: list[bytes] = []
        silence_ms = 0
        speech_ms  = 0
        in_speech  = False

        while self.running:
            # Khi bot đang nói → đọc bỏ PCM để tránh echo tự kích hoạt
            if self._speaking.is_set():
                try:
                    self._proc.stdout.read(CHUNK_BYTES)
                except Exception:
                    break
                continue

            try:
                data = self._proc.stdout.read(CHUNK_BYTES)
            except Exception:
                break
            if not data:
                break

            arr = np.frombuffer(data, dtype=np.int16)
            rms = float(np.sqrt(np.mean(arr.astype(np.float32) ** 2)))
            is_speech = rms > self.vad_threshold

            if is_speech:
                silence_ms = 0
                speech_ms += CHUNK_MS
                speech_frames.append(data)
                if not in_speech and speech_ms >= 200:
                    in_speech = True
                    self.on_log("🎤 Đang nghe khách nói...")
            else:
                if in_speech:
                    silence_ms += CHUNK_MS
                    speech_frames.append(data)   # giữ phần cuối có silence

                    if silence_ms >= SILENCE_END_MS:
                        # Kết thúc câu nói → xử lý
                        self._dispatch(speech_frames, speech_ms)
                        speech_frames = []
                        silence_ms = speech_ms = 0
                        in_speech = False

                    elif speech_ms + silence_ms >= MAX_RECORD_MS:
                        # Quá dài → cắt và gửi ngay
                        self._dispatch(speech_frames, speech_ms)
                        speech_frames = []
                        silence_ms = speech_ms = 0
                        in_speech = False
                else:
                    # Chưa vào speech → reset counter ngắn
                    if speech_ms < 200:
                        speech_ms = 0
                        speech_frames.clear()

    def _dispatch(self, frames: list[bytes], speech_ms: int):
        """Gửi audio vào pipeline xử lý nếu đủ dài."""
        if speech_ms < SPEECH_MIN_MS:
            return
        audio = b"".join(frames)
        threading.Thread(
            target=self._process_utterance,
            args=(audio,),
            daemon=True,
        ).start()

    # ── Utterance pipeline ────────────────────────────────────────────────────

    def _process_utterance(self, pcm_data: bytes):
        """PCM → WAV → STT → GPT → TTS → loa camera."""
        # Đặt cờ ngay để ngăn VAD echo trong lúc xử lý
        self._speaking.set()
        try:
            # 1. PCM → WAV
            wav_path = self._pcm_to_wav(pcm_data)
            if not wav_path:
                return

            # 2. STT (Google Speech Recognition — miễn phí)
            self.on_log("🔄 Đang nhận dạng giọng nói...")
            text = self._stt(wav_path)
            try:
                os.unlink(wav_path)
            except Exception:
                pass

            if not text or len(text.strip()) < 2:
                self.on_log("⚠ Không nhận dạng được giọng nói.")
                return

            self.on_log(f"💬 Khách: {text}")

            # 3. Rule-based response
            reply = self._responder.respond(text)

            if not reply:
                return  # Không khớp từ khóa → im lặng

            self.on_log(f"🔊 Bot: {reply}")
            self.on_transcript(text, reply)

            # 4. TTS
            mp3_path = self._tts(reply)
            if not mp3_path:
                return

            # 5. Phát qua loa camera
            self._play_through_camera(mp3_path)

            try:
                os.unlink(mp3_path)
            except Exception:
                pass

        finally:
            self._speaking.clear()

    # ── Step 1: PCM → WAV ─────────────────────────────────────────────────────

    def _pcm_to_wav(self, pcm_data: bytes) -> Optional[str]:
        """Ghi raw PCM s16le → WAV file tạm (dùng module wave có sẵn)."""
        try:
            fd, path = tempfile.mkstemp(suffix=".wav")
            os.close(fd)
            with wave.open(path, "wb") as wf:
                wf.setnchannels(CHANNELS)
                wf.setsampwidth(2)        # 16-bit = 2 bytes/sample
                wf.setframerate(SAMPLE_RATE)
                wf.writeframes(pcm_data)
            return path
        except Exception as e:
            self.on_log(f"❌ Lỗi tạo WAV: {e}")
            return None

    # ── Step 2: Whisper STT ───────────────────────────────────────────────────

    def _stt(self, wav_path: str) -> Optional[str]:
        """Google Speech Recognition (miễn phí, không cần API key)."""
        try:
            import speech_recognition as sr
        except ImportError:
            self.on_log("❌ Chưa cài SpeechRecognition: pip install SpeechRecognition")
            return None
        r = sr.Recognizer()
        r.energy_threshold = 200
        try:
            with sr.AudioFile(wav_path) as source:
                audio = r.record(source)
            return r.recognize_google(audio, language="vi-VN").strip()
        except sr.UnknownValueError:
            return None
        except Exception as e:
            self.on_log(f"❌ Lỗi Google STT: {e}")
            return None


    # ── Step 4: TTS ───────────────────────────────────────────────────────────

    def _tts(self, text: str) -> Optional[str]:
        """Google TTS (gTTS, miễn phí) → MP3."""
        try:
            from gtts import gTTS
        except ImportError:
            self.on_log("❌ gTTS chưa cài: pip install gtts")
            return None

        try:
            fd, path = tempfile.mkstemp(suffix=".mp3")
            os.close(fd)
            gTTS(text=text, lang="vi").save(path)
            if os.path.getsize(path) == 0:
                self.on_log("❌ gTTS: file âm thanh trống")
                return None
            return path
        except Exception as e:
            self.on_log(f"❌ Lỗi gTTS: {e}")
            return None


    # ── Step 5: Play through camera ───────────────────────────────────────────

    def _play_through_camera(self, mp3_path: str):
        """Phát MP3 qua loa camera bằng AudioTalker (RTSP back-channel)."""
        from talker import AudioTalker
        talker = AudioTalker(
            camera_ip=self.camera_ip,
            http_port=self.http_port,
            rtsp_port=self.rtsp_port,
            username=self.username,
            password=self.password,
            rtsp_path=self.rtsp_path,
            mic_index=0,
            on_log=self.on_log,
        )
        talker.start_file(mp3_path)
        # Chờ phát xong, tối đa 60 giây
        deadline = time.monotonic() + 60.0
        while talker.running and time.monotonic() < deadline:
            time.sleep(0.2)
        talker.stop()
