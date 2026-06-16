"""
listener.py - Nhận âm thanh từ camera EZVIZ qua RTSP stream
Sử dụng FFmpeg để giải mã và PyAudio để phát.
"""
import os
import shutil
import subprocess
import threading
import sys
import time
from datetime import datetime, timedelta

import numpy as np
import pyaudio


def _find_ffmpeg() -> str:
    """Trả về đường dẫn ffmpeg.exe, ưu tiên PATH rồi thử vị trí winget mặc định."""
    # 1. Tìm trong PATH hiện tại
    found = shutil.which("ffmpeg")
    if found:
        return found

    # 2. Thêm thư mục bin của winget vào PATH trong process này rồi thử lại
    winget_base = os.path.join(
        os.environ.get("LOCALAPPDATA", ""),
        "Microsoft", "WinGet", "Packages",
    )
    if os.path.isdir(winget_base):
        for entry in os.listdir(winget_base):
            if entry.lower().startswith("gyan.ffmpeg"):
                candidate = os.path.join(winget_base, entry)
                for root, _dirs, files in os.walk(candidate):
                    if "ffmpeg.exe" in files:
                        os.environ["PATH"] = root + os.pathsep + os.environ["PATH"]
                        return os.path.join(root, "ffmpeg.exe")

    return "ffmpeg"  # fallback — sẽ báo lỗi rõ ràng nếu thực sự không có

CHUNK = 2048
SAMPLE_RATE = 16000
CHANNELS = 1


class AudioListener:
    def __init__(self, rtsp_url: str, on_volume=None, on_log=None, on_status=None):
        self.rtsp_url = rtsp_url
        self.on_volume = on_volume
        self.on_log = on_log
        self.on_status = on_status
        self.running = False
        self.process = None
        self._pa = pyaudio.PyAudio()
        self._max_retries = 10
        self._retry_delay = 2  # seconds, will exponentially backoff
        self._daily_reconnect_thread = None
        self._daily_reconnect_hour = 7

    # ------------------------------------------------------------------ public
    def start(self):
        if self.running:
            return
        self.running = True
        self._daily_reconnect_thread = threading.Thread(
            target=self._daily_reconnect_worker,
            daemon=True,
        )
        self._daily_reconnect_thread.start()
        t = threading.Thread(target=self._run, daemon=True)
        t.start()

    def stop(self):
        self.running = False
        if self.process:
            try:
                self.process.terminate()
            except Exception:
                pass

    def close(self):
        self.stop()
        self._pa.terminate()

    # ----------------------------------------------------------------- private
    def _run(self):
        retry_count = 0
        retry_delay = self._retry_delay
        
        while self.running:
            try:
                self._attempt_connection()
                # If connection succeeds, reset retry counter
                retry_count = 0
                retry_delay = self._retry_delay
            except Exception as exc:
                if not self.running:
                    break
                    
                retry_count += 1
                if retry_count > self._max_retries:
                    self._notify_log(f"❌ Không thể kết nối sau {self._max_retries} lần thử. Dừng bot.")
                    self.running = False
                    break
                
                self._notify_log(f"⚠ Lỗi kết nối (lần {retry_count}/{self._max_retries}): {exc}")
                self._notify_log(f"🔄 Sẽ thử kết nối lại trong {retry_delay} giây...")
                self._notify_status("reconnecting")
                
                # Wait before retry with exponential backoff
                for _ in range(int(retry_delay)):
                    if not self.running:
                        return
                    time.sleep(1)
                
                # Exponential backoff: cap at 30 seconds
                retry_delay = min(retry_delay * 1.5, 30)
        
        self._notify_status("disconnected")
        self._notify_log("Đã ngắt kết nối nghe.")

    def _attempt_connection(self):
        """Attempt one connection to RTSP stream."""
        cmd = [
            _find_ffmpeg(),
            "-loglevel", "error",
            "-rtsp_transport", "tcp",
            "-i", self.rtsp_url,
            "-vn",
            "-acodec", "pcm_s16le",
            "-ar", str(SAMPLE_RATE),
            "-ac", str(CHANNELS),
            "-f", "s16le",
            "-avoid_negative_ts", "make_zero",
            "pipe:1",
        ]

        kwargs = {}
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

        stream = None
        try:
            self.process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                **kwargs,
            )

            stream = self._pa.open(
                format=pyaudio.paInt16,
                channels=CHANNELS,
                rate=SAMPLE_RATE,
                output=True,
                frames_per_buffer=CHUNK,
            )

            self._notify_status("connected")
            self._notify_log("✅ Đã kết nối camera. Đang phát âm thanh...")

            while self.running:
                data = self.process.stdout.read(CHUNK * 2)
                if not data:
                    # Stream ended, will trigger reconnection
                    raise RuntimeError("RTSP stream closed unexpectedly")
                stream.write(data)
                if self.on_volume:
                    arr = np.frombuffer(data, dtype=np.int16)
                    vol = int(np.abs(arr).mean() / 327)  # → 0-100
                    self.on_volume(min(vol, 100))

        except FileNotFoundError:
            self._notify_log(
                "❌ Không tìm thấy 'ffmpeg'. Hãy cài FFmpeg và thêm vào PATH hệ thống."
            )
            raise
        finally:
            if stream:
                try:
                    stream.stop_stream()
                    stream.close()
                except Exception:
                    pass
            if self.process:
                try:
                    self.process.terminate()
                except Exception:
                    pass

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

            self._notify_log("⏰ 07:00 - Tự động ngắt để kết nối lại RTSP...")
            if self.process:
                try:
                    self.process.terminate()
                except Exception:
                    pass

    def _notify_log(self, msg: str):
        if self.on_log:
            self.on_log(msg)

    def _notify_status(self, status: str):
        if self.on_status:
            self.on_status(status)
