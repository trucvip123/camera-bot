"""
main.py - Giao diện đồ hoạ (Tkinter) điều khiển âm thanh camera EZVIZ
"""
import time
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk

import pyaudio

from bot import DEFAULT_QA_TEXT, StoreBot
from listener import AudioListener
from talker import AudioTalker


class CameraBotApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("CameraBot – EZVIZ Audio Control")
        self.root.resizable(True, True)

        self.listener: AudioListener | None = None
        self.talker:   AudioTalker  | None = None
        self.bot:      StoreBot     | None = None
        self._pa = pyaudio.PyAudio()

        self._build_ui()

    # ================================================================== UI build
    def _build_ui(self):
        PAD = {"padx": 10, "pady": 5}

        # ── Camera settings ──────────────────────────────────────────────────
        sf = ttk.LabelFrame(self.root, text=" Cài đặt Camera EZVIZ ", padding=10)
        sf.pack(fill="x", **PAD)

        labels = ["IP Camera:", "HTTP Port:", "RTSP Port:", "Username:", "Password:", "RTSP Path:"]
        self.ip_var        = tk.StringVar(value="192.168.110.235")
        self.http_port_var = tk.StringVar(value="80")
        self.rtsp_port_var = tk.StringVar(value="554")
        self.user_var      = tk.StringVar(value="admin")
        self.pass_var      = tk.StringVar(value="PVWLBZ")
        self.rtsp_path_var = tk.StringVar(value="/Streaming/Channels/101")

        row_cfg = [
            (0, 0, self.ip_var,        dict(width=18)),
            (0, 2, self.http_port_var, dict(width=7)),
            (1, 0, self.rtsp_port_var, dict(width=7)),
            (2, 0, self.user_var,      dict(width=18)),
            (2, 2, self.pass_var,      dict(width=16, show="*")),
        ]

        label_col = {
            (0, 0): "IP Camera:",  (0, 2): "HTTP Port:",
            (1, 0): "RTSP Port:",
            (2, 0): "Username:",   (2, 2): "Password:",
        }
        for (row, col), text in label_col.items():
            ttk.Label(sf, text=text).grid(row=row, column=col, sticky="w", pady=3, padx=(0, 2))

        for row, col, var, kw in row_cfg:
            ttk.Entry(sf, textvariable=var, **kw).grid(row=row, column=col + 1, sticky="w", padx=(0, 8))

        ttk.Label(sf, text="RTSP Path:").grid(row=3, column=0, sticky="w", pady=3)
        ttk.Entry(sf, textvariable=self.rtsp_path_var, width=46).grid(
            row=3, column=1, columnspan=4, sticky="w"
        )

        ttk.Label(sf, text="Sub-stream: /Streaming/Channels/102  |  Luồng chính: /Streaming/Channels/101",
                  foreground="gray", font=("Arial", 8)).grid(
            row=4, column=0, columnspan=5, sticky="w"
        )

        # ── Microphone ────────────────────────────────────────────────────────
        mf = ttk.LabelFrame(self.root, text=" Microphone (dùng cho chức năng Nói) ", padding=8)
        mf.pack(fill="x", **PAD)

        self.mic_var   = tk.StringVar()
        self.mic_combo = ttk.Combobox(mf, textvariable=self.mic_var, width=48, state="readonly")
        self.mic_combo.pack(side="left", padx=(0, 6))
        ttk.Button(mf, text="🔄 Làm mới", command=self._refresh_mics).pack(side="left")
        self._refresh_mics()

        # ── Control buttons ───────────────────────────────────────────────────
        cf = tk.Frame(self.root, bg="#f0f0f0")
        cf.pack(fill="x", padx=10, pady=6)

        self.listen_btn = tk.Button(
            cf, text="🎧  Nghe Camera",
            command=self._toggle_listen,
            bg="#1976D2", fg="white",
            font=("Arial", 11, "bold"),
            relief="flat", padx=16, pady=9, cursor="hand2",
        )
        self.listen_btn.pack(side="left", padx=(0, 8))

        self.talk_btn = tk.Button(
            cf, text="🎤  Nói Qua Loa",
            command=self._toggle_talk,
            bg="#388E3C", fg="white",
            font=("Arial", 11, "bold"),
            relief="flat", padx=16, pady=9, cursor="hand2",
        )
        self.talk_btn.pack(side="left", padx=(0, 8))

        self.file_btn = tk.Button(
            cf, text="📂  Phát File",
            command=self._toggle_file,
            bg="#7B1FA2", fg="white",
            font=("Arial", 11, "bold"),
            relief="flat", padx=16, pady=9, cursor="hand2",
        )
        self.file_btn.pack(side="left")

        # Status dots
        self.listen_dot = tk.Label(cf, text="●", fg="lightgray", font=("Arial", 18), bg="#f0f0f0")
        self.listen_dot.pack(side="left", padx=(18, 2))
        tk.Label(cf, text="Nghe", bg="#f0f0f0").pack(side="left")

        self.talk_dot = tk.Label(cf, text="●", fg="lightgray", font=("Arial", 18), bg="#f0f0f0")
        self.talk_dot.pack(side="left", padx=(12, 2))
        tk.Label(cf, text="Nói/File", bg="#f0f0f0").pack(side="left")

        # ── Volume bar ────────────────────────────────────────────────────────
        vf = ttk.LabelFrame(self.root, text=" Mức âm từ camera ", padding=6)
        vf.pack(fill="x", **PAD)
        self.vol_bar = ttk.Progressbar(vf, length=500, mode="determinate", maximum=100)
        self.vol_bar.pack(fill="x")

        # ── Bot Chào Khách ───────────────────────────────────────────────────────────
        botf = ttk.LabelFrame(
            self.root, text=" 🤖 Bot Chào Khách (✅ Miễn phí — Không cần API key) ", padding=8)
        botf.pack(fill="x", **PAD)
        botf.columnconfigure(1, weight=1)

        # Free mode notice
        ttk.Label(
            botf,
            text="STT: Google Speech Recognition (miễn phí)   |   TTS: Microsoft edge-tts (miễn phí)",
            foreground="#2E7D32", font=("Arial", 8),
        ).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 4))

        # Q&A configuration
        ttk.Label(botf, text="Kịch bản Q&A:").grid(
            row=1, column=0, sticky="nw", pady=(2, 2))
        self.prompt_text = tk.Text(
            botf, height=6, width=44, font=("Consolas", 8), wrap="word")
        self.prompt_text.grid(
            row=1, column=1, columnspan=2, sticky="we", padx=(4, 0), pady=(2, 2))
        self.prompt_text.insert("1.0", DEFAULT_QA_TEXT)

        # VAD threshold
        ttk.Label(botf, text="Ngưỡng tiếng (ôn):").grid(
            row=2, column=0, sticky="w", pady=2)
        self.vad_var = tk.DoubleVar(value=500)
        self.vad_label = ttk.Label(botf, text="500", width=5)
        self.vad_var.trace_add(
            "write", lambda *_: self.vad_label.config(
                text=str(int(self.vad_var.get()))))
        ttk.Scale(
            botf, from_=50, to=2000, variable=self.vad_var,
            orient="horizontal", length=180,
        ).grid(row=2, column=1, sticky="w", padx=(4, 4))
        self.vad_label.grid(row=2, column=2, sticky="w")

        # Bot button + status dot
        bot_btn_row = tk.Frame(botf)
        bot_btn_row.grid(row=3, column=0, columnspan=3, sticky="w", pady=(6, 2))
        self.bot_btn = tk.Button(
            bot_btn_row, text="🤖  Bật Bot",
            command=self._toggle_bot,
            bg="#F57C00", fg="white",
            font=("Arial", 11, "bold"),
            relief="flat", padx=16, pady=8, cursor="hand2",
        )
        self.bot_btn.pack(side="left")
        self.bot_dot = tk.Label(
            bot_btn_row, text="●", fg="lightgray",
            font=("Arial", 18))
        self.bot_dot.pack(side="left", padx=(10, 4))
        ttk.Label(bot_btn_row, text="Bot").pack(side="left")

        ttk.Label(botf, text="Hội thoại:", anchor="w").grid(
            row=4, column=0, sticky="nw", pady=(4, 0))
        self.transcript_box = scrolledtext.ScrolledText(
            botf, height=4, state="disabled",
            font=("Consolas", 9), wrap="word",
        )
        self.transcript_box.grid(
            row=4, column=1, columnspan=2, sticky="we",
            padx=(4, 0), pady=(4, 0))

        # ── Log ──────────────────────────────────────────────────────────────────────────
        lf = ttk.LabelFrame(self.root, text=" Nhật ký hoạt động ", padding=5)
        lf.pack(fill="both", expand=True, padx=10, pady=(4, 10))
        self.log_box = scrolledtext.ScrolledText(
            lf, height=7, state="disabled", font=("Consolas", 9)
        )
        self.log_box.pack(fill="both", expand=True)

        self._log("👋 Sẵn sàng. Điền thông tin camera rồi nhấn Nghe / Nói / Phát File.")
        self._log("ℹ️  Nghe: dùng FFmpeg qua RTSP.")
        self._log("ℹ️  Nói/File: thử RTSP back-channel → ISAPI (LAN) theo thứ tự.")
        self._log("ℹ️  Bot: nhấn Bật Bot — MIỄN PHÍ, không cần API key. Chỉnh sửa Kịch bản Q&A theo cửa hàng.")

    # ================================================================== helpers
    def _refresh_mics(self):
        mics = []
        for i in range(self._pa.get_device_count()):
            info = self._pa.get_device_info_by_index(i)
            if info["maxInputChannels"] > 0:
                mics.append(f"{i}: {info['name']}")
        self.mic_combo["values"] = mics
        if mics:
            self.mic_combo.current(0)

    def _log(self, msg: str):
        def _do():
            self.log_box.config(state="normal")
            self.log_box.insert("end", f"[{time.strftime('%H:%M:%S')}] {msg}\n")
            self.log_box.see("end")
            self.log_box.config(state="disabled")
        self.root.after(0, _do)

    def _rtsp_url(self) -> str:
        path = self.rtsp_path_var.get().strip()
        if not path.startswith("/"):
            path = "/" + path
        return (
            f"rtsp://{self.user_var.get().strip()}:{self.pass_var.get()}"
            f"@{self.ip_var.get().strip()}:{self.rtsp_port_var.get().strip()}{path}"
        )

    # ================================================================== listen
    def _toggle_listen(self):
        if self.listener and self.listener.running:
            self.listener.stop()
        else:
            self.listener = AudioListener(
                rtsp_url=self._rtsp_url(),
                on_volume=lambda v: self.root.after(0, lambda: self.vol_bar.config(value=v)),
                on_log=self._log,
                on_status=self._on_listen_status,
            )
            self.listener.start()
            self._set_listen_btn(active=True)

    def _on_listen_status(self, status: str):
        connected = status == "connected"
        self.root.after(0, lambda: self.listen_dot.config(fg="#43A047" if connected else "lightgray"))
        if not connected:
            self.root.after(0, lambda: self._set_listen_btn(active=False))

    def _set_listen_btn(self, active: bool):
        if active:
            self.listen_btn.config(text="⏹  Dừng Nghe", bg="#D32F2F")
        else:
            self.listen_btn.config(text="🎧  Nghe Camera", bg="#1976D2")

    # ================================================================== talk
    def _toggle_talk(self):
        if self.talker and self.talker.running:
            self.talker.stop()
        else:
            mic_text = self.mic_var.get()
            if not mic_text:
                messagebox.showwarning("Thiếu microphone", "Vui lòng chọn microphone!")
                return
            mic_index = int(mic_text.split(":")[0])

            self.talker = AudioTalker(
                camera_ip=self.ip_var.get().strip(),
                http_port=int(self.http_port_var.get().strip()),
                rtsp_port=int(self.rtsp_port_var.get().strip()),
                username=self.user_var.get().strip(),
                password=self.pass_var.get().strip(),
                rtsp_path=self.rtsp_path_var.get().strip() or "/Streaming/Channels/101",
                mic_index=mic_index,
                on_log=self._log,
                on_status=self._on_talk_status,
            )
            self.talker.start()
            self._set_talk_btn(active=True)

    def _on_talk_status(self, status: str):
        connected = status == "connected"
        self.root.after(0, lambda: self.talk_dot.config(fg="#43A047" if connected else "lightgray"))
        if not connected:
            self.root.after(0, lambda: self._set_talk_btn(active=False))

    def _set_talk_btn(self, active: bool):
        if active:
            self.talk_btn.config(text="⏹  Dừng Nói", bg="#D32F2F")
            self.file_btn.config(state="disabled")
        else:
            self.talk_btn.config(text="🎤  Nói Qua Loa", bg="#388E3C")
            self.file_btn.config(state="normal")

    # ================================================================== file play
    def _toggle_file(self):
        if self.talker and self.talker.running:
            self.talker.stop()
            return

        file_path = filedialog.askopenfilename(
            title="Chọn file âm thanh",
            filetypes=[
                ("Audio files", "*.wav *.mp3 *.aac *.ogg *.flac *.m4a *.wma"),
                ("All files", "*.*"),
            ],
        )
        if not file_path:
            return

        self.talker = AudioTalker(
            camera_ip=self.ip_var.get().strip(),
            http_port=int(self.http_port_var.get().strip()),
            rtsp_port=int(self.rtsp_port_var.get().strip()),
            username=self.user_var.get().strip(),
            password=self.pass_var.get().strip(),
            rtsp_path=self.rtsp_path_var.get().strip() or "/Streaming/Channels/101",
            on_log=self._log,
            on_status=self._on_file_status,
        )
        self.talker.start_file(file_path)
        self.file_btn.config(text="⏹  Dừng File", bg="#D32F2F")
        self.talk_btn.config(state="disabled")

    def _on_file_status(self, status: str):
        connected = status == "connected"
        self.root.after(
            0,
            lambda: self.talk_dot.config(fg="#7B1FA2" if connected else "lightgray"),
        )
        if not connected:
            self.root.after(0, self._reset_file_btn)

    def _reset_file_btn(self):
        self.file_btn.config(text="📂  Phát File", bg="#7B1FA2", state="normal")
        self.talk_btn.config(state="normal")

    # ================================================================== bot
    def _toggle_bot(self):
        try:
            if self.bot and self.bot.running:
                self.bot.stop()
                self.bot = None
                return

            qa_text = self.prompt_text.get("1.0", "end").strip()

            self.bot = StoreBot(
                camera_ip=self.ip_var.get().strip(),
                rtsp_port=int(self.rtsp_port_var.get().strip()),
                http_port=int(self.http_port_var.get().strip()),
                username=self.user_var.get().strip(),
                password=self.pass_var.get().strip(),
                rtsp_path=self.rtsp_path_var.get().strip() or "/Streaming/Channels/101",
                qa_text=qa_text,
                vad_threshold=int(self.vad_var.get()),
                on_log=self._log,
                on_status=self._on_bot_status,
                on_transcript=self._on_bot_transcript,
            )
            self.bot.start()
            self._set_bot_btn(active=True)
        except Exception as exc:
            self._log(f"❌ Lỗi khởi động bot: {exc}")
            import traceback
            self._log(traceback.format_exc())

    def _on_bot_status(self, status: str):
        active = status == "listening"
        self.root.after(
            0,
            lambda: self.bot_dot.config(fg="#43A047" if active else "lightgray"),
        )
        if not active:
            self.root.after(0, lambda: self._set_bot_btn(active=False))

    def _set_bot_btn(self, active: bool):
        if active:
            self.bot_btn.config(text="⏹  Dừng Bot", bg="#D32F2F")
        else:
            self.bot_btn.config(text="🤖  Bật Bot", bg="#F57C00")

    def _on_bot_transcript(self, question: str, answer: str):
        def _do():
            self.transcript_box.config(state="normal")
            self.transcript_box.insert(
                "end",
                f"[{time.strftime('%H:%M:%S')}] 👤 {question}\n"
                f"                   🤖 {answer}\n"
                f"{'-'*50}\n",
            )
            self.transcript_box.see("end")
            self.transcript_box.config(state="disabled")
        self.root.after(0, _do)

    # ================================================================== close
    def on_close(self):
        if self.listener:
            self.listener.stop()
        if self.talker:
            self.talker.stop()
        if self.bot:
            self.bot.stop()
        self._pa.terminate()
        self.root.destroy()


# ======================================================================== entry
if __name__ == "__main__":
    root = tk.Tk()
    app = CameraBotApp(root)
    root.protocol("WM_DELETE_WINDOW", app.on_close)
    root.mainloop()
