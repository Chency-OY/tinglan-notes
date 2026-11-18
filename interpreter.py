"""Bilingual microphone subtitles for Windows."""
from __future__ import annotations

import base64
from contextlib import contextmanager
import hashlib
import hmac
import io
import json
import os
import queue
import secrets
import sqlite3
import sys
import threading
import time
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from urllib.parse import urlencode
import uuid
import wave

import httpx
import keyring
import numpy as np
import sounddevice as sd
import websocket

SERVICE = "zh-en-live-interpreter"
SAMPLE_RATE = 16000
FRAME_SAMPLES = 3200  # 200 ms
DATA_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "ZhEnInterpreter"


def clock_text(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S")


class HistoryStore:
    def __init__(self, path: Path | None = None):
        self.path = path or DATA_DIR / "history.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS sessions (
                    id INTEGER PRIMARY KEY, started REAL NOT NULL, ended REAL,
                    provider TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS segments (
                    id INTEGER PRIMARY KEY, session_id INTEGER NOT NULL,
                    started REAL NOT NULL, ended REAL NOT NULL,
                    source TEXT NOT NULL, target TEXT NOT NULL,
                    FOREIGN KEY(session_id) REFERENCES sessions(id)
                );
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        try:
            with db:
                yield db
        finally:
            db.close()

    def begin(self, provider: str) -> int:
        with self.connect() as db:
            cursor = db.execute("INSERT INTO sessions(started,provider) VALUES (?,?)", (time.time(), provider))
            return cursor.lastrowid

    def end(self, session_id: int):
        with self.connect() as db:
            db.execute("UPDATE sessions SET ended=? WHERE id=?", (time.time(), session_id))

    def resume(self, session_id: int):
        with self.connect() as db:
            db.execute("UPDATE sessions SET ended=NULL WHERE id=?", (session_id,))

    def append(self, session_id: int, started: float, ended: float, source: str, target: str):
        with self.connect() as db:
            db.execute("INSERT INTO segments(session_id,started,ended,source,target) VALUES (?,?,?,?,?)", (session_id, started, ended, source, target))

    def sessions(self):
        with self.connect() as db:
            return db.execute("SELECT id,started,ended,provider FROM sessions ORDER BY started DESC").fetchall()

    def segments(self, session_id: int):
        with self.connect() as db:
            return db.execute("SELECT started,ended,source,target FROM segments WHERE session_id=? ORDER BY id", (session_id,)).fetchall()


def wav_bytes(pcm: bytes) -> bytes:
    out = io.BytesIO()
    with wave.open(out, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(SAMPLE_RATE)
        wav.writeframes(pcm)
    return out.getvalue()


def tencent_url(appid: str, secret_id: str, secret_key: str) -> str:
    now = int(time.time())
    path = f"asr.cloud.tencent.com/asr/speech_translate/{appid}"
    params = {
        "expired": now + 3600,
        "nonce": secrets.randbelow(2**31),
        "secretid": secret_id,
        "source": "zh_en",
        "target": "zh_en",
        "timestamp": now,
        "trans_model": "hunyuan-translation-lite",
        "voice_format": 1,
        "voice_id": str(uuid.uuid4()),
    }
    query = urlencode(sorted(params.items()))
    signature = base64.b64encode(hmac.new(secret_key.encode(), f"{path}?{query}".encode(), hashlib.sha1).digest()).decode()
    return f"wss://{path}?{query}&signature={urlencode({'signature': signature}).split('=', 1)[1]}"


class Segmenter:
    """Emit speech after a short pause, or every four seconds of continuous speech."""

    def __init__(self):
        self.parts: list[bytes] = []
        self.speech_frames = 0
        self.quiet_frames = 0

    def feed(self, frame: bytes) -> bytes | None:
        samples = np.frombuffer(frame, dtype=np.int16).astype(np.float32)
        loud = float(np.sqrt(np.mean(samples * samples))) > 450
        if loud:
            self.speech_frames += 1
            self.quiet_frames = 0
        elif self.parts:
            self.quiet_frames += 1
        if loud or self.parts:
            self.parts.append(frame)
        if self.parts and (self.quiet_frames >= 3 or len(self.parts) >= 20):
            return self.flush()
        return None

    def flush(self) -> bytes | None:
        result = b"".join(self.parts) if self.speech_frames >= 2 else None
        self.parts, self.speech_frames, self.quiet_frames = [], 0, 0
        return result


class Interpreter(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("中英同传字幕")
        icon_path = Path(getattr(sys, "_MEIPASS", Path(__file__).parent)) / "icon.ico"
        if icon_path.exists():
            self.iconbitmap(str(icon_path))
        self.geometry("960x720+180+80")
        self.minsize(650, 500)
        self.configure(bg="#f7f7ff")
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.mode = tk.StringVar(value="DeepSeek 近实时")
        self.device = tk.StringVar()
        self.status = tk.StringVar(value="未启动")
        self.events: queue.Queue[tuple] = queue.Queue()
        self.audio: queue.Queue[bytes] = queue.Queue(maxsize=100)
        self.stop_event = threading.Event()
        self.worker: threading.Thread | None = None
        self.stream = None
        self.live_sentence = ""
        self.zoom = 1.0
        self.opacity = tk.DoubleVar(value=.85)
        self.history_store = HistoryStore()
        self.session_id: int | None = None
        self.capture_started = 0.0
        self.closing = False
        self._build()
        self._build_overlay()
        self.after(80, self.poll)

    def _build(self):
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("TCombobox", padding=7, fieldbackground="#ffffff", background="#ffffff", foreground="#50496d")
        style.configure("Treeview", background="#ffffff", fieldbackground="#ffffff", foreground="#50496d", rowheight=28)
        style.configure("Treeview.Heading", background="#eeeafd", foreground="#514873", font=("Microsoft YaHei UI", 9, "bold"))

        header = tk.Frame(self, bg="#f7f7ff")
        header.pack(fill="x", padx=24, pady=(20, 14))
        tk.Label(header, text="✦ 中英同传", bg="#f7f7ff", fg="#655b9b", font=("Microsoft YaHei UI", 20, "bold")).pack(side="left")
        tk.Label(header, text="  把每一句都好好记下来", bg="#f7f7ff", fg="#9b97af", font=("Microsoft YaHei UI", 10)).pack(side="left", pady=(9, 0))
        zoom_bar = tk.Frame(header, bg="#f7f7ff")
        zoom_bar.pack(side="right")
        for text, action in (("−", lambda: self.set_zoom(-.1)), ("100%", lambda: self.set_zoom(0)), ("+", lambda: self.set_zoom(.1))):
            tk.Button(zoom_bar, text=text, command=action, bg="#eae6fb", fg="#665b94", activebackground="#dcd4fa", relief="flat", bd=0, padx=10, pady=5, cursor="hand2").pack(side="left", padx=2)
        tk.Button(zoom_bar, text="☰  历史", command=self.show_history, bg="#eae6fb", fg="#665b94", activebackground="#dcd4fa", relief="flat", bd=0, padx=12, pady=5, cursor="hand2").pack(side="left", padx=(9, 0))

        bar = tk.Frame(self, bg="#ffffff", highlightbackground="#e7e1f6", highlightthickness=1)
        bar.pack(fill="x", padx=22, pady=(0, 14))
        inner = tk.Frame(bar, bg="#ffffff")
        inner.pack(fill="x", padx=16, pady=13)
        tk.Label(inner, text="翻译模式", bg="#ffffff", fg="#8882a3", font=("Microsoft YaHei UI", 9)).grid(row=0, column=0, sticky="w")
        tk.Label(inner, text="麦克风", bg="#ffffff", fg="#8882a3", font=("Microsoft YaHei UI", 9)).grid(row=0, column=1, sticky="w", padx=(10, 0))
        self.mode_combo = ttk.Combobox(inner, textvariable=self.mode, values=["DeepSeek 近实时", "腾讯云 实时"], state="readonly", width=17)
        self.mode_combo.grid(row=1, column=0, sticky="ew", pady=(5, 0))
        devices = [(i, d["name"]) for i, d in enumerate(sd.query_devices()) if d["max_input_channels"] > 0]
        self.devices = {f"{i}: {name}": i for i, name in devices}
        names = list(self.devices)
        self.device.set(names[0] if names else "无可用麦克风")
        self.device_combo = ttk.Combobox(inner, textvariable=self.device, values=names, state="readonly", width=24)
        self.device_combo.grid(row=1, column=1, sticky="ew", padx=(10, 0), pady=(5, 0))
        self.start_button = tk.Button(inner, text="●  开始", command=self.toggle, bg="#d8caff", fg="#514579", activebackground="#cbb8fa", relief="flat", bd=0, padx=16, pady=7, font=("Microsoft YaHei UI", 10, "bold"), cursor="hand2")
        self.start_button.grid(row=1, column=2, padx=(12, 0), pady=(5, 0))
        tk.Button(inner, text="⚙  设置", command=self.settings, bg="#e8f3fa", fg="#52738b", activebackground="#d8eefa", relief="flat", bd=0, padx=12, pady=7, cursor="hand2").grid(row=1, column=3, padx=(8, 0), pady=(5, 0))
        inner.grid_columnconfigure(0, weight=1)
        inner.grid_columnconfigure(1, weight=2)

        cards = tk.Frame(self, bg="#f7f7ff")
        cards.pack(fill="x", padx=22, pady=(0, 14))
        source_card = tk.Frame(cards, bg="#eff7fc", highlightbackground="#dceaf4", highlightthickness=1)
        source_card.pack(fill="x", pady=(0, 9))
        tk.Label(source_card, text="原文  ORIGINAL", bg="#eff7fc", fg="#6689a0", font=("Microsoft YaHei UI", 9, "bold")).pack(anchor="w", padx=16, pady=(11, 3))
        self.source = tk.Label(source_card, text="说话后，原文会显示在这里", anchor="w", justify="left", bg="#eff7fc", fg="#667789", font=("Microsoft YaHei UI", 12))
        self.source.pack(fill="x", padx=16, pady=(0, 13))
        target_card = tk.Frame(cards, bg="#f0edff", highlightbackground="#dfd8fa", highlightthickness=1)
        target_card.pack(fill="x")
        tk.Label(target_card, text="译文  TRANSLATION", bg="#f0edff", fg="#8a77b3", font=("Microsoft YaHei UI", 9, "bold")).pack(anchor="w", padx=16, pady=(11, 3))
        self.target = tk.Label(target_card, text="翻译结果会显示在这里", anchor="w", justify="left", bg="#f0edff", fg="#62588d", font=("Microsoft YaHei UI", 17, "bold"))
        self.target.pack(fill="x", padx=16, pady=(0, 13))

        transcript_header = tk.Frame(self, bg="#f7f7ff")
        transcript_header.pack(fill="x", padx=24, pady=(2, 8))
        tk.Label(transcript_header, text="整场对话", bg="#f7f7ff", fg="#665b91", font=("Microsoft YaHei UI", 13, "bold")).pack(side="left")
        self.session_label = tk.Label(transcript_header, text="本次新会话", bg="#f7f7ff", fg="#aaa2bb", font=("Microsoft YaHei UI", 9))
        self.session_label.pack(side="left", padx=12, pady=(3, 0))
        tk.Button(transcript_header, text="＋ 新会话", command=self.start_new_session, bg="#fbeaf2", fg="#a77390", activebackground="#f7dce8", relief="flat", padx=10, pady=4, cursor="hand2").pack(side="right")

        transcript_frame = tk.Frame(self, bg="#ffffff", highlightbackground="#e7e1f6", highlightthickness=1)
        transcript_frame.pack(fill="both", expand=True, padx=22, pady=(0, 12))
        scrollbar = ttk.Scrollbar(transcript_frame)
        scrollbar.pack(side="right", fill="y")
        self.transcript = tk.Text(transcript_frame, wrap="word", bg="#ffffff", fg="#595473", insertbackground="#7668a5", relief="flat", padx=18, pady=16, font=("Microsoft YaHei UI", 10), yscrollcommand=scrollbar.set, state="disabled")
        self.transcript.pack(fill="both", expand=True)
        scrollbar.configure(command=self.transcript.yview)
        self.transcript.tag_configure("time", foreground="#a397ba", font=("Microsoft YaHei UI", 9))
        self.transcript.tag_configure("original", foreground="#63849a")
        self.transcript.tag_configure("translation", foreground="#675994", font=("Microsoft YaHei UI", 11, "bold"))
        self.transcript.tag_configure("empty", foreground="#aaa2bb")
        self._empty_transcript()

        footer = tk.Frame(self, bg="#f7f7ff")
        footer.pack(fill="x", padx=24, pady=(0, 16))
        tk.Label(footer, text="●", bg="#f7f7ff", fg="#ab9ae5", font=("Segoe UI", 10)).pack(side="left")
        tk.Label(footer, textvariable=self.status, bg="#f7f7ff", fg="#8d88a5", font=("Microsoft YaHei UI", 9)).pack(side="left", padx=(5, 0))
        tk.Button(footer, text="显示 / 隐藏悬浮字幕", command=self.toggle_overlay, bg="#eae6fb", fg="#665b94", activebackground="#dcd4fa", relief="flat", padx=12, pady=5, cursor="hand2").pack(side="right")
        self.hint_label = tk.Label(footer, text="拖动边缘改变窗口大小  ·  Ctrl + 滚轮调字幕", bg="#f7f7ff", fg="#aaa2bb", font=("Microsoft YaHei UI", 9))
        self.hint_label.pack(side="right", padx=(0, 12))
        self.bind("<Configure>", self.on_resize)
        self.bind("<Control-MouseWheel>", lambda event: self.set_zoom(.1 if event.delta > 0 else -.1))
        self.after_idle(self.on_resize)

    def _build_overlay(self):
        self.overlay = tk.Toplevel(self)
        self.overlay.title("同传悬浮字幕")
        self.overlay.geometry("640x220+400+70")
        self.overlay.minsize(400, 145)
        self.overlay.configure(bg="#f5f1ff")
        self.overlay.attributes("-topmost", True)
        self.overlay.attributes("-alpha", self.opacity.get())
        self.overlay.protocol("WM_DELETE_WINDOW", self.overlay.withdraw)
        top = tk.Frame(self.overlay, bg="#f5f1ff")
        top.pack(fill="x", padx=15, pady=(10, 3))
        tk.Label(top, text="✦ 同传字幕", bg="#f5f1ff", fg="#776aa8", font=("Microsoft YaHei UI", 10, "bold")).pack(side="left")
        tk.Button(top, text="×", command=self.overlay.withdraw, bg="#f5f1ff", fg="#988db7", activebackground="#e9e1fa", relief="flat", font=("Segoe UI", 13), cursor="hand2").pack(side="right")
        tk.Scale(top, from_=.55, to=1.0, resolution=.05, orient="horizontal", variable=self.opacity,
                 command=lambda value: self.overlay.attributes("-alpha", float(value)), length=110,
                 showvalue=False, bg="#f5f1ff", activebackground="#bda9ec", troughcolor="#ddd2f2",
                 highlightthickness=0, bd=0).pack(side="right", padx=(4, 0))
        tk.Label(top, text="透明度", bg="#f5f1ff", fg="#988db7", font=("Microsoft YaHei UI", 9)).pack(side="right")
        self.overlay_source = tk.Label(self.overlay, text="原文会显示在这里", bg="#f5f1ff", fg="#697e91", anchor="w", justify="left", font=("Microsoft YaHei UI", 12))
        self.overlay_source.pack(fill="x", padx=19, pady=(3, 4))
        self.overlay_target = tk.Label(self.overlay, text="翻译会显示在这里", bg="#f5f1ff", fg="#63568e", anchor="w", justify="left", font=("Microsoft YaHei UI", 20, "bold"))
        self.overlay_target.pack(fill="both", expand=True, padx=19, pady=(0, 12))
        self.overlay.bind("<Configure>", self.on_overlay_resize)
        self.overlay.bind("<Control-MouseWheel>", lambda event: self.set_zoom(.1 if event.delta > 0 else -.1))
        self.after_idle(self.on_overlay_resize)

    def toggle_overlay(self):
        if self.overlay.state() == "withdrawn":
            self.overlay.deiconify()
            self.overlay.lift()
        else:
            self.overlay.withdraw()

    def _empty_transcript(self):
        self.transcript.configure(state="normal")
        self.transcript.delete("1.0", "end")
        self.transcript.insert("end", "开始说话后，这里会按时间整理整场对话。\n", "empty")
        self.transcript.configure(state="disabled")

    def append_transcript(self, started, ended, source, target):
        self.transcript.configure(state="normal")
        if self.transcript.get("1.0", "1.end").startswith("开始说话后"):
            self.transcript.delete("1.0", "end")
        self.transcript.insert("end", f"{clock_text(started)}  →  {clock_text(ended)}\n", "time")
        self.transcript.insert("end", f"原文  {source}\n", "original")
        self.transcript.insert("end", f"译文  {target}\n\n", "translation")
        self.transcript.configure(state="disabled")
        self.transcript.see("end")

    def load_session(self, sid):
        self.session_id = sid
        self._empty_transcript()
        rows = self.history_store.segments(sid)
        for row in rows:
            self.append_transcript(*row)
        self.session_label.configure(text=f"历史会话 #{sid} · {len(rows)} 段")
        self.status.set(f"已选择历史会话 #{sid}，点击开始后继续记录")

    def start_new_session(self):
        if self.worker and self.worker.is_alive():
            messagebox.showinfo("请先暂停", "先暂停当前录音，再新建会话。", parent=self)
            return
        self.session_id = None
        self._empty_transcript()
        self.session_label.configure(text="本次新会话")
        self.source.configure(text="说话后，原文会显示在这里")
        self.target.configure(text="翻译结果会显示在这里")
        self.overlay_source.configure(text="原文会显示在这里")
        self.overlay_target.configure(text="翻译会显示在这里")
        self.status.set("下一次开始将创建新会话")

    def set_zoom(self, change):
        self.zoom = 1.0 if change == 0 else min(1.8, max(.7, self.zoom + change))
        self.on_resize()
        self.on_overlay_resize()

    def on_resize(self, event=None):
        width, height = self.winfo_width(), self.winfo_height()
        factor = min(width / 960, height / 720) * self.zoom
        self.source.configure(font=("Microsoft YaHei UI", max(10, min(21, round(12 * factor)))), wraplength=max(400, width - 86))
        self.target.configure(font=("Microsoft YaHei UI", max(14, min(30, round(17 * factor))), "bold"), wraplength=max(400, width - 86))
        if width < 850:
            self.hint_label.pack_forget()
        elif not self.hint_label.winfo_manager():
            self.hint_label.pack(side="right", padx=(0, 10))

    def on_overlay_resize(self, event=None):
        if not hasattr(self, "overlay"):
            return
        width, height = self.overlay.winfo_width(), self.overlay.winfo_height()
        factor = min(width / 640, height / 220) * self.zoom
        self.overlay_source.configure(font=("Microsoft YaHei UI", max(10, min(22, round(12 * factor)))), wraplength=max(320, width - 44))
        self.overlay_target.configure(font=("Microsoft YaHei UI", max(15, min(36, round(20 * factor))), "bold"), wraplength=max(320, width - 44))

    def show_history(self):
        dialog = tk.Toplevel(self)
        dialog.title("会话历史")
        dialog.geometry("790x520")
        dialog.minsize(600, 380)
        dialog.attributes("-topmost", True)
        dialog.configure(bg="#f7f7ff")
        tk.Label(dialog, text="✦ 会话历史", bg="#f7f7ff", fg="#665b91", font=("Microsoft YaHei UI", 17, "bold")).pack(anchor="w", padx=18, pady=(14, 8))
        tree = ttk.Treeview(dialog, columns=("start", "end", "provider"), show="headings", height=7)
        for col, title, width in (("start", "开始时间", 180), ("end", "结束时间", 180), ("provider", "服务", 150)):
            tree.heading(col, text=title)
            tree.column(col, width=width)
        tree.pack(fill="x", padx=18)
        for sid, started, ended, provider in self.history_store.sessions():
            tree.insert("", "end", iid=str(sid), values=(clock_text(started), clock_text(ended) if ended else "进行中", provider))
        detail = tk.Text(dialog, wrap="word", bg="#ffffff", fg="#5f5879", insertbackground="#665b91", relief="flat", padx=14, pady=12, font=("Microsoft YaHei UI", 10))
        detail.pack(fill="both", expand=True, padx=18, pady=10)
        detail.configure(state="disabled")
        def selected_id():
            selected = tree.selection()
            return int(selected[0]) if selected else None
        def show_selected(_=None):
            sid = selected_id()
            detail.configure(state="normal")
            detail.delete("1.0", "end")
            if sid is not None:
                for start, end, source, target in self.history_store.segments(sid):
                    detail.insert("end", f"[{clock_text(start)} – {clock_text(end)}]\n原文：{source}\n译文：{target}\n\n")
            detail.configure(state="disabled")
        tree.bind("<<TreeviewSelect>>", show_selected)
        actions = tk.Frame(dialog, bg="#f7f7ff")
        actions.pack(fill="x", padx=18, pady=(0, 14))
        def continue_session():
            sid = selected_id()
            if sid is None: return
            if self.worker and self.worker.is_alive():
                messagebox.showinfo("请先暂停", "先暂停当前录音，再选择要继续的历史会话。", parent=dialog)
                return
            self.load_session(sid)
            dialog.destroy()
        def new_session():
            if self.worker and self.worker.is_alive():
                messagebox.showinfo("请先暂停", "先暂停当前录音，再新建会话。", parent=dialog)
                return
            self.start_new_session()
            dialog.destroy()
        def export():
            sid = selected_id()
            if sid is None: return
            folder = filedialog.askdirectory(parent=dialog, title="选择导出文件夹")
            if not folder: return
            rows = self.history_store.segments(sid)
            session = next(row for row in self.history_store.sessions() if row[0] == sid)
            stem = f"会话_{datetime.fromtimestamp(session[1]):%Y%m%d_%H%M%S}_{sid}"
            for suffix, index in (("原文", 2), ("译文", 3)):
                lines = [f"开始：{clock_text(session[1])}", f"结束：{clock_text(session[2]) if session[2] else '进行中'}", ""]
                lines += [f"[{clock_text(start)} – {clock_text(end)}] {row[index]}" for row in rows for start, end in [(row[0], row[1])]]
                (Path(folder) / f"{stem}_{suffix}.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
            messagebox.showinfo("导出完成", "已分别保存原文和译文两个 TXT 文件。", parent=dialog)
        for label, command in (("继续此会话", continue_session), ("新会话", new_session), ("分别导出原文和译文", export)):
            tk.Button(actions, text=label, command=command, bg="#eae6fb", fg="#665b94", activebackground="#dcd4fa", relief="flat", padx=12, pady=7).pack(side="left", padx=(0, 8))

    def settings(self):
        dialog = tk.Toplevel(self)
        dialog.title("服务密钥")
        dialog.geometry("540x320")
        dialog.attributes("-topmost", True)
        dialog.configure(bg="#f7f7ff")
        tk.Label(dialog, text="✦ 服务设置", bg="#f7f7ff", fg="#665b91", font=("Microsoft YaHei UI", 15, "bold")).grid(row=0, column=0, columnspan=2, sticky="w", padx=20, pady=(18, 10))
        fields = [("DeepSeek API Key", "deepseek_key"), ("腾讯云 AppID", "tencent_appid"), ("腾讯云 SecretId", "tencent_id"), ("腾讯云 SecretKey", "tencent_key")]
        entries = {}
        for row, (label, account) in enumerate(fields):
            tk.Label(dialog, text=label, bg="#f7f7ff", fg="#736d8e").grid(row=row+1, column=0, padx=(20, 8), pady=9, sticky="w")
            entry = ttk.Entry(dialog, width=43, show="*" if "Key" in label or "Secret" in label else "")
            entry.grid(row=row+1, column=1, padx=(4, 20), pady=9)
            entry.insert(0, keyring.get_password(SERVICE, account) or "")
            entries[account] = entry
        def save():
            for account, entry in entries.items():
                value = entry.get().strip()
                if value:
                    keyring.set_password(SERVICE, account, value)
                else:
                    try: keyring.delete_password(SERVICE, account)
                    except keyring.errors.PasswordDeleteError: pass
            dialog.destroy()
        tk.Button(dialog, text="保存设置", command=save, bg="#d8caff", fg="#514579", activebackground="#cbb8fa", relief="flat", padx=18, pady=7).grid(row=5, column=1, sticky="e", padx=20, pady=15)

    def toggle(self):
        if self.worker and self.worker.is_alive():
            self.stop_event.set()
            if self.stream: self.stream.stop(); self.stream.close(); self.stream = None
            if self.session_id is not None: self.history_store.end(self.session_id)
            self.start_button.configure(text="●  开始", bg="#d8caff")
            self.mode_combo.configure(state="readonly")
            self.device_combo.configure(state="readonly")
            self.status.set("正在停止")
            return
        credentials = (["deepseek_key"] if self.mode.get().startswith("DeepSeek") else ["tencent_appid", "tencent_id", "tencent_key"])
        if any(not keyring.get_password(SERVICE, x) for x in credentials):
            messagebox.showinfo("需要密钥", "请先在“密钥设置”中填写当前服务的凭据。")
            return
        if self.device.get() not in self.devices:
            messagebox.showerror("麦克风", "没有可用的麦克风。")
            return
        self.stop_event.clear()
        self.audio = queue.Queue(maxsize=100)
        try:
            self.stream = sd.RawInputStream(samplerate=SAMPLE_RATE, channels=1, dtype="int16", blocksize=FRAME_SAMPLES, device=self.devices[self.device.get()], callback=self.capture)
            self.stream.start()
        except Exception as exc:
            self.stream = None
            messagebox.showerror("麦克风启动失败", str(exc))
            return
        target = self.deepseek_loop if self.mode.get().startswith("DeepSeek") else self.tencent_loop
        self.worker = threading.Thread(target=target, daemon=True)
        self.capture_started = time.time()
        if self.session_id is None:
            self.session_id = self.history_store.begin(self.mode.get())
            self.session_label.configure(text=f"本次会话 #{self.session_id} · {clock_text(self.capture_started)}")
        else:
            self.history_store.resume(self.session_id)
        self.worker.start()
        self.start_button.configure(text="Ⅱ  暂停", bg="#f3bd63")
        self.mode_combo.configure(state="disabled")
        self.device_combo.configure(state="disabled")
        self.status.set("正在收音")

    def capture(self, indata, frames, timing, status):
        if status: self.events.put(("status", str(status)))
        if not self.stop_event.is_set():
            try: self.audio.put_nowait(bytes(indata))
            except queue.Full: self.events.put(("status", "处理较慢，已丢弃部分音频"))

    def deepseek_loop(self):
        session_id = self.session_id
        segmenter = Segmenter()
        jobs: queue.Queue[tuple[bytes, float, float]] = queue.Queue(maxsize=5)
        def process():
            key = keyring.get_password(SERVICE, "deepseek_key")
            with httpx.Client(timeout=30) as client:
                while not self.stop_event.is_set() or not jobs.empty():
                    try: pcm, started, ended = jobs.get(timeout=.2)
                    except queue.Empty: continue
                    try:
                        r = client.post("https://api.deepseek.com/v1/tools/asr", headers={"Authorization": f"Bearer {key}"}, files={"file": ("speech.wav", wav_bytes(pcm), "audio/wav")})
                        r.raise_for_status()
                        source = r.json().get("text", "").strip()
                        if not source: continue
                        self.events.put(("source", source))
                        r = client.post("https://api.deepseek.com/chat/completions", headers={"Authorization": f"Bearer {key}"}, json={"model":"deepseek-flash","thinking":{"type":"disabled"},"messages":[{"role":"system","content":"Translate the user's speech to the opposite language: mainly Chinese to English, mainly English to Simplified Chinese. Return only the translation. Preserve names and meaning."},{"role":"user","content":source}],"stream":False})
                        r.raise_for_status()
                        target = r.json()["choices"][0]["message"]["content"].strip()
                        self.events.put(("final_pair", started, ended, source, target, session_id))
                    except Exception as exc:
                        self.events.put(("status", f"DeepSeek 请求失败：{type(exc).__name__}"))
        processor = threading.Thread(target=process, daemon=True)
        processor.start()
        while not self.stop_event.is_set():
            try: frame = self.audio.get(timeout=.2)
            except queue.Empty: continue
            pcm = segmenter.feed(frame)
            if pcm:
                ended = time.time()
                try: jobs.put_nowait((pcm, ended - len(pcm) / (SAMPLE_RATE * 2), ended))
                except queue.Full: self.events.put(("status", "处理较慢，跳过一段语音"))
        last = segmenter.flush()
        if last:
            ended = time.time()
            try: jobs.put_nowait((last, ended - len(last) / (SAMPLE_RATE * 2), ended))
            except queue.Full: pass
        processor.join()
        self.events.put(("status", "已暂停"))

    def tencent_loop(self):
        session_id = self.session_id
        ws = None
        try:
            creds = [keyring.get_password(SERVICE, x) for x in ("tencent_appid", "tencent_id", "tencent_key")]
            ws = websocket.create_connection(tencent_url(*creds), timeout=10)
            ws.settimeout(1)
            finished = threading.Event()
            def receive():
                while not finished.is_set():
                    try: message = ws.recv()
                    except websocket.WebSocketTimeoutException: continue
                    except Exception: break
                    if not isinstance(message, str): continue
                    data = json.loads(message)
                    if data.get("code", 0) != 0:
                        self.events.put(("status", f"腾讯云错误：{data.get('message', data['code'])}"))
                        self.stop_event.set(); break
                    if data.get("final") == 1:
                        finished.set(); break
                    result = data.get("result")
                    if result:
                        started = self.capture_started + result.get("start_time", 0) / 1000
                        ended = self.capture_started + result.get("end_time", 0) / 1000
                        kind = "final_pair" if result.get("sentence_end") else "pair"
                        self.events.put((kind, started, ended, result.get("source_text", ""), result.get("target_text", ""), session_id))
            listener = threading.Thread(target=receive, daemon=True)
            listener.start()
            while not self.stop_event.is_set():
                try: ws.send(self.audio.get(timeout=.3), opcode=websocket.ABNF.OPCODE_BINARY)
                except queue.Empty: continue
            try: ws.send(json.dumps({"type":"end"}))
            except Exception: pass
            listener.join(timeout=4)
            finished.set()
        except Exception as exc:
            self.events.put(("status", f"腾讯云连接失败：{type(exc).__name__}"))
        finally:
            if ws: ws.close()
            self.events.put(("stopped",))

    def poll(self):
        try:
            while True:
                event = self.events.get_nowait()
                if event[0] in ("pair", "final_pair"):
                    self.source.configure(text=event[3]); self.target.configure(text=event[4])
                    self.overlay_source.configure(text=event[3]); self.overlay_target.configure(text=event[4])
                    if event[0] == "final_pair" and event[5] is not None and (event[3] or event[4]):
                        self.history_store.append(event[5], event[1], event[2], event[3], event[4])
                        if event[5] == self.session_id:
                            self.append_transcript(event[1], event[2], event[3], event[4])
                elif event[0] == "source":
                    self.source.configure(text=event[1]); self.target.configure(text="翻译中…")
                    self.overlay_source.configure(text=event[1]); self.overlay_target.configure(text="翻译中…")
                elif event[0] == "status": self.status.set(event[1])
                elif event[0] == "stopped":
                    if self.stream: self.stream.stop(); self.stream.close(); self.stream = None
                    if self.session_id is not None: self.history_store.end(self.session_id)
                    self.start_button.configure(text="●  开始", bg="#d8caff")
                    self.mode_combo.configure(state="readonly")
                    self.device_combo.configure(state="readonly")
        except queue.Empty: pass
        self.after(80, self.poll)

    def close(self):
        if self.closing:
            return
        self.closing = True
        self.stop_event.set()
        if self.stream: self.stream.stop(); self.stream.close(); self.stream = None
        self.status.set("正在保存最后一段…")
        self.finish_close()

    def finish_close(self):
        if self.worker and self.worker.is_alive():
            self.after(150, self.finish_close)
            return
        if not self.events.empty():
            self.after(150, self.finish_close)
            return
        if self.session_id is not None: self.history_store.end(self.session_id)
        self.destroy()


if __name__ == "__main__":
    Interpreter().mainloop()
