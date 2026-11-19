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
import re
import tempfile
import zipfile
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


def safe_filename(text):
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', text)[:60].strip(' .') or '未命名'


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
                CREATE TABLE IF NOT EXISTS projects (
                    id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE COLLATE NOCASE
                );
                CREATE TABLE IF NOT EXISTS preferences (
                    name TEXT PRIMARY KEY, value TEXT NOT NULL
                );
            """)
            columns = {row[1] for row in db.execute("PRAGMA table_info(sessions)")}
            if "project_id" not in columns:
                db.execute("ALTER TABLE sessions ADD COLUMN project_id INTEGER REFERENCES projects(id)")
            if "archived" not in columns:
                db.execute("ALTER TABLE sessions ADD COLUMN archived INTEGER NOT NULL DEFAULT 0")
            db.execute("CREATE INDEX IF NOT EXISTS segments_by_session ON segments(session_id,id)")
            if "title" not in columns:
                db.execute("ALTER TABLE sessions ADD COLUMN title TEXT")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        try:
            db.execute("PRAGMA foreign_keys=ON")
            with db:
                yield db
        finally:
            db.close()

    def begin(self, provider: str, title=None, project_id=None) -> int:
        with self.connect() as db:
            cursor = db.execute("INSERT INTO sessions(started,provider,title,project_id) VALUES (?,?,?,?)", (time.time(), provider, title, project_id))
            return cursor.lastrowid

    def end(self, session_id: int, ended=None):
        with self.connect() as db:
            db.execute("UPDATE sessions SET ended=? WHERE id=?", (time.time() if ended is None else ended, session_id))

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

    def last_translation(self, session_id: int):
        with self.connect() as db:
            return db.execute("""
                SELECT source,target FROM segments WHERE session_id=? AND source<>'' AND target<>''
                ORDER BY id DESC LIMIT 1
            """, (session_id,)).fetchone()

    def session(self, session_id: int):
        with self.connect() as db:
            return db.execute("SELECT id,started,ended,provider FROM sessions WHERE id=?", (session_id,)).fetchone()

    def session_previews(self, project_id=None, archived=False):
        with self.connect() as db:
            return db.execute("""SELECT id,started,title FROM sessions WHERE archived=?
                AND (? IS NULL OR project_id=?) ORDER BY started DESC,id DESC""",
                (int(archived), project_id, project_id)).fetchall()

    def library_sessions(self):
        with self.connect() as db:
            return db.execute("""
                SELECT id,started,title,project_id,archived FROM sessions ORDER BY started DESC,id DESC
            """).fetchall()

    def projects(self):
        with self.connect() as db:
            return db.execute("SELECT id,name FROM projects ORDER BY name COLLATE NOCASE,id").fetchall()

    def create_project(self, name):
        name = name.strip()
        if not 1 <= len(name) <= 80:
            raise ValueError("项目名请输入 1 至 80 个字")
        with self.connect() as db:
            try:
                return db.execute("INSERT INTO projects(name) VALUES (?)", (name,)).lastrowid
            except sqlite3.IntegrityError as exc:
                raise ValueError("已有同名项目") from exc

    def rename_project(self, project_id, name):
        name = name.strip()
        if not 1 <= len(name) <= 80:
            raise ValueError("项目名请输入 1 至 80 个字")
        with self.connect() as db:
            try:
                db.execute("UPDATE projects SET name=? WHERE id=?", (name, project_id))
            except sqlite3.IntegrityError as exc:
                raise ValueError("已有同名项目") from exc

    def move_session(self, sid, project_id):
        with self.connect() as db:
            db.execute("UPDATE sessions SET project_id=? WHERE id=?", (project_id,sid))

    def archive_session(self, sid, archived=True):
        with self.connect() as db:
            db.execute("UPDATE sessions SET archived=? WHERE id=?", (int(archived),sid))

    def project_zip(self, pid, destination):
        # Take one consistent snapshot, then release SQLite before compressing files.
        with self.connect() as db:
            db.execute("BEGIN")
            sessions = db.execute("SELECT id,started,ended,title FROM sessions WHERE project_id=? ORDER BY started,id", (pid,)).fetchall()
            if not sessions:
                raise ValueError("项目里还没有会话")
            segments = db.execute("""SELECT g.session_id,g.started,g.ended,g.source,g.target FROM segments g
                JOIN sessions s ON s.id=g.session_id WHERE s.project_id=? ORDER BY g.session_id,g.id""", (pid,)).fetchall()
        grouped = {}
        for sid, *row in segments:
            grouped.setdefault(sid, []).append(row)
        dest = Path(destination)
        with tempfile.NamedTemporaryFile(dir=dest.parent, suffix=".tmp", delete=False) as tmp:
            temp = Path(tmp.name)
        try:
            with zipfile.ZipFile(temp, "w", zipfile.ZIP_DEFLATED) as archive:
                for sid, start, end, title in sessions:
                    date = datetime.fromtimestamp(start)
                    title = title or date.strftime("%m.%d %H:%M")
                    folder = f"{date:%Y%m%d_%H%M%S}_{sid}_{safe_filename(title)}"
                    header = [title, f"开始：{clock_text(start)}", f"结束：{clock_text(end) if end else '进行中'}", ""]
                    for suffix, col in (("原文",2),("译文",3)):
                        lines = header + [f"[{clock_text(r[0])} – {clock_text(r[1])}] {r[col]}" for r in grouped.get(sid, ())]
                        archive.writestr(f"{folder}/{suffix}.txt", ("\r\n".join(lines)+"\r\n").encode("utf-8-sig"))
            os.replace(temp, dest)
        finally:
            temp.unlink(missing_ok=True)
        return len(sessions)

    def title(self, sid):
        with self.connect() as db:
            title, started = db.execute("SELECT title,started FROM sessions WHERE id=?", (sid,)).fetchone()
            return title or datetime.fromtimestamp(started).strftime("%m.%d %H:%M")

    def rename(self, sid, title):
        with self.connect() as db:
            db.execute("UPDATE sessions SET title=? WHERE id=?", (title or None, sid))

    def delete_session(self, sid):
        with self.connect() as db:
            db.execute("DELETE FROM segments WHERE session_id=?", (sid,))
            db.execute("DELETE FROM sessions WHERE id=?", (sid,))

    def preference(self, name, default):
        with self.connect() as db:
            row = db.execute("SELECT value FROM preferences WHERE name=?", (name,)).fetchone()
            return row[0] if row else default

    def set_preference(self, name, value):
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO preferences VALUES (?,?)", (name, value))


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


class PillButton(tk.Canvas):
    def __init__(self, parent, text, command, width=92, height=34, fill="#c8d3f6", foreground="#3a3360", background="#f1f6ff"):
        super().__init__(parent, width=width, height=height, bg=background, highlightthickness=0, bd=0, cursor="hand2")
        self.button_width, self.button_height = width, height
        self.foreground = foreground
        self.command = command
        self.set_label(text, fill)
        self.bind("<Button-1>", lambda event: self.command())

    def set_label(self, text, fill):
        self.delete("all")
        h, w = self.button_height, self.button_width
        r = h / 2
        self.create_oval(0, 0, h, h, fill=fill, outline=fill)
        self.create_oval(w - h, 0, w, h, fill=fill, outline=fill)
        self.create_rectangle(r, 0, w - r, h, fill=fill, outline=fill)
        self.create_text(w / 2, h / 2, text=text, fill=self.foreground, font=("Microsoft YaHei UI", 10, "bold"))


class Interpreter(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("中英同传字幕")
        icon_path = Path(getattr(sys, "_MEIPASS", Path(__file__).parent)) / "icon.ico"
        if icon_path.exists():
            self.iconbitmap(str(icon_path))
        self.geometry("720x320+300+100")
        self.minsize(540, 260)
        self.configure(bg="#f1f6ff")
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
        self.opacity = tk.DoubleVar(value=.88)
        self.history_store = HistoryStore()
        self.session_id: int | None = None
        self.capture_started = 0.0
        self.closing = False
        self._build()
        self.attributes("-topmost", True)
        self.attributes("-alpha", self.opacity.get())
        self.after(80, self.poll)

    # The compact subtitles and the conversation browser are views of one window.
    def _build(self):
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("TCombobox", padding=7, fieldbackground="#ffffff", background="#ffffff", foreground="#3a3360")
        devices = [(i, d["name"]) for i, d in enumerate(sd.query_devices()) if d["max_input_channels"] > 0]
        self.devices = {f"{i}: {name}": i for i, name in devices}
        names = list(self.devices)
        self.device.set(names[0] if names else "无可用麦克风")
        self.active_view = "subtitles"
        self.compact_view = tk.Frame(self, bg="#f1f6ff")
        self.compact_view.pack(fill="both", expand=True)
        top = tk.Frame(self.compact_view, bg="#f1f6ff")
        top.pack(fill="x", padx=18, pady=(12, 8))
        tk.Label(top, text="✦ 同传字幕", bg="#f1f6ff", fg="#536a9b", font=("Microsoft YaHei UI", 12, "bold")).pack(side="left")
        self.session_label = tk.Label(top, text="新会话", bg="#f1f6ff", fg="#8d9bbd", font=("Microsoft YaHei UI", 9))
        self.session_label.pack(side="left", padx=(9, 0))
        PillButton(top, "历史", self.show_history, width=72, fill="#dce6fb").pack(side="right", padx=(7, 0))
        PillButton(top, "设置", self.settings, width=72, fill="#dce6fb").pack(side="right")
        controls = tk.Frame(self.compact_view, bg="#f1f6ff")
        controls.pack(fill="x", padx=19, pady=(0, 10))
        self.mode_combo = ttk.Combobox(controls, textvariable=self.mode, values=["DeepSeek 近实时", "腾讯云 实时"], state="readonly", width=15)
        self.mode_combo.pack(side="left", padx=(0, 8))
        self.device_combo = ttk.Combobox(controls, textvariable=self.device, values=names, state="readonly", width=22)
        self.device_combo.pack(side="left", fill="x", expand=True)
        self.start_button = PillButton(controls, "● 开始", self.toggle, width=91, fill="#c8d3f6")
        self.start_button.pack(side="right", padx=(9, 0))
        source_card = tk.Frame(self.compact_view, bg="#eaf2ff")
        source_card.pack(fill="x", padx=18, pady=(0, 7))
        tk.Label(source_card, text="原文", bg="#eaf2ff", fg="#7385ad", font=("Microsoft YaHei UI", 9, "bold")).pack(anchor="w", padx=13, pady=(8, 2))
        self.source = tk.Label(source_card, text="原文会显示在这里", bg="#eaf2ff", fg="#5c719c", anchor="w", justify="left", font=("Microsoft YaHei UI", 12))
        self.source.pack(fill="x", padx=13, pady=(0, 9))
        target_card = tk.Frame(self.compact_view, bg="#dfe9ff")
        target_card.pack(fill="both", expand=True, padx=18, pady=(0, 7))
        tk.Label(target_card, text="译文", bg="#dfe9ff", fg="#6c7fac", font=("Microsoft YaHei UI", 9, "bold")).pack(anchor="w", padx=13, pady=(8, 2))
        self.target = tk.Label(target_card, text="翻译会显示在这里", bg="#dfe9ff", fg="#3a3360", anchor="w", justify="left", font=("Microsoft YaHei UI", 19, "bold"))
        self.target.pack(fill="both", expand=True, padx=13, pady=(0, 9))
        footer = tk.Frame(self.compact_view, bg="#f1f6ff")
        footer.pack(fill="x", padx=19, pady=(0, 9))
        tk.Label(footer, textvariable=self.status, bg="#f1f6ff", fg="#8291af", font=("Microsoft YaHei UI", 9)).pack(side="left")
        tk.Scale(footer, from_=.55, to=1.0, resolution=.05, orient="horizontal", variable=self.opacity,
                 command=lambda value: self.attributes("-alpha", float(value)) if self.active_view == "subtitles" else None,
                 showvalue=False, length=110, bg="#f1f6ff", activebackground="#abb3e2", troughcolor="#d5dff6", highlightthickness=0, bd=0).pack(side="right")
        tk.Label(footer, text="透明度", bg="#f1f6ff", fg="#8d9bbd", font=("Microsoft YaHei UI", 9)).pack(side="right")
        self.bind("<Configure>", self.on_resize)
        self.bind("<Control-MouseWheel>", lambda event: self.set_zoom(.1 if event.delta > 0 else -.1))
        self.after_idle(self.on_resize)

    def set_zoom(self, change):
        self.zoom = 1.0 if change == 0 else min(1.8, max(.7, self.zoom + change))
        self.on_resize()

    def on_resize(self, event=None):
        if self.active_view != "subtitles":
            return
        width, height = self.winfo_width(), self.winfo_height()
        factor = min(width / 720, height / 320) * self.zoom
        self.source.configure(font=("Microsoft YaHei UI", max(10, min(22, round(12 * factor)))), wraplength=max(380, width - 65))
        self.target.configure(font=("Microsoft YaHei UI", max(15, min(36, round(19 * factor))), "bold"), wraplength=max(380, width - 65))

    def _open_page(self, name):
        self.compact_geometry = self.geometry()
        self.compact_view.pack_forget()
        self.active_view = name
        self.attributes("-topmost", False)
        self.attributes("-alpha", 1.0)
        self.minsize(760, 500)
        self.geometry("1000x650+180+70")
        self.page_view = tk.Frame(self, bg="#f8faff")
        self.page_view.pack(fill="both", expand=True)
        return self.page_view

    def back_to_subtitles(self):
        self.page_view.destroy()
        self.active_view = "subtitles"
        self.minsize(540, 260)
        self.geometry(self.compact_geometry)
        self.compact_view.pack(fill="both", expand=True)
        self.attributes("-alpha", self.opacity.get())
        self.attributes("-topmost", True)
        self.on_resize()

    def show_history(self):
        page = self._open_page("history")
        header = tk.Frame(page, bg="#f8faff")
        header.pack(fill="x", padx=24, pady=(20, 14))
        PillButton(header, "← 返回", self.back_to_subtitles, width=90, fill="#dce6fb", background="#f8faff").pack(side="left")
        tk.Label(header, text="历史对话", bg="#f8faff", fg="#3a3360", font=("Microsoft YaHei UI", 19, "bold")).pack(side="left", padx=(17, 0))
        body = tk.Frame(page, bg="#f8faff")
        body.pack(fill="both", expand=True, padx=22, pady=(0, 20))
        sidebar = tk.Frame(body, bg="#edf3ff", width=250)
        sidebar.pack(side="left", fill="y", padx=(0, 12))
        sidebar.pack_propagate(False)
        tk.Label(sidebar, text="全部会话", bg="#edf3ff", fg="#536a9b", font=("Microsoft YaHei UI", 12, "bold")).pack(anchor="w", padx=16, pady=(17, 10))
        sessions = self.history_store.sessions()
        session_list = tk.Listbox(sidebar, bg="#edf3ff", fg="#536a9b", selectbackground="#c8d3f6", selectforeground="#3a3360", relief="flat", borderwidth=0, highlightthickness=0, activestyle="none", font=("Microsoft YaHei UI", 10))
        session_list.pack(fill="both", expand=True, padx=10, pady=(0, 12))
        for sid, started, ended, provider in sessions:
            session_list.insert("end", f"  {datetime.fromtimestamp(started):%m月%d日 %H:%M} · {provider}")
        def new_session():
            if self.worker and self.worker.is_alive():
                self.start_new_session()
                return
            self.start_new_session()
            self.back_to_subtitles()
        PillButton(sidebar, "＋ 新会话", new_session, width=205, fill="#f4e8e8", background="#edf3ff").pack(pady=(0, 16))

        content = tk.Frame(body, bg="#ffffff", highlightbackground="#dfe7fb", highlightthickness=1)
        content.pack(side="left", fill="both", expand=True)
        heading = tk.Label(content, text="选择左侧会话", bg="#ffffff", fg="#3a3360", font=("Microsoft YaHei UI", 14, "bold"))
        heading.pack(anchor="w", padx=22, pady=(18, 3))
        meta = tk.Label(content, text="整场对话会显示在这里", bg="#ffffff", fg="#8d9bbd", font=("Microsoft YaHei UI", 9))
        meta.pack(anchor="w", padx=22, pady=(0, 12))
        detail = tk.Text(content, wrap="word", bg="#ffffff", fg="#4f5e85", relief="flat", padx=22, pady=14, font=("Microsoft YaHei UI", 11), state="disabled")
        detail.pack(fill="both", expand=True)
        detail.tag_configure("time", foreground="#9ca9c2", font=("Microsoft YaHei UI", 9))
        detail.tag_configure("source", background="#edf3ff", foreground="#4f5e85", spacing1=8, spacing3=8)
        detail.tag_configure("target", background="#dfe9ff", foreground="#3a3360", font=("Microsoft YaHei UI", 11, "bold"), spacing1=8, spacing3=8)
        selected_sid = [None]
        def render_selected(_=None):
            selection = session_list.curselection()
            sid = sessions[selection[0]][0] if selection else None
            selected_sid[0] = sid
            detail.configure(state="normal")
            detail.delete("1.0", "end")
            if sid is not None:
                row = sessions[selection[0]]
                heading.configure(text=f"会话 #{sid}")
                meta.configure(text=f"{clock_text(row[1])}  —  {clock_text(row[2]) if row[2] else '进行中'}")
                for start, end, source, target in self.history_store.segments(sid):
                    detail.insert("end", f"{clock_text(start)}  →  {clock_text(end)}\n", "time")
                    detail.insert("end", f"  原文  {source}\n", "source")
                    detail.insert("end", f"  译文  {target}\n\n", "target")
            detail.configure(state="disabled")
        session_list.bind("<<ListboxSelect>>", render_selected)
        self.history_refresh = render_selected
        if sessions:
            preferred = next((i for i, row in enumerate(sessions) if row[0] == self.session_id), 0)
            session_list.selection_set(preferred)
            render_selected()
        actions = tk.Frame(content, bg="#ffffff")
        actions.pack(fill="x", padx=22, pady=(8, 16))
        def continue_session():
            if selected_sid[0] is None:
                return
            if self.worker and self.worker.is_alive():
                messagebox.showinfo("请先暂停", "先暂停当前录音，再继续历史会话。", parent=self)
                return
            self.load_session(selected_sid[0])
            self.back_to_subtitles()
        def export():
            sid = selected_sid[0]
            if sid is None:
                return
            folder = filedialog.askdirectory(parent=self, title="选择导出文件夹")
            if not folder:
                return
            rows = self.history_store.segments(sid)
            session = next(row for row in self.history_store.sessions() if row[0] == sid)
            stem = f"会话_{datetime.fromtimestamp(session[1]):%Y%m%d_%H%M%S}_{sid}"
            for suffix, index in (("原文", 2), ("译文", 3)):
                lines = [f"开始：{clock_text(session[1])}", f"结束：{clock_text(session[2]) if session[2] else '进行中'}", ""]
                lines += [f"[{clock_text(row[0])} – {clock_text(row[1])}] {row[index]}" for row in rows]
                (Path(folder) / f"{stem}_{suffix}.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
            messagebox.showinfo("导出完成", "已分别保存原文和译文两个 TXT 文件。", parent=self)
        PillButton(actions, "继续此会话", continue_session, width=128, fill="#dce6fb", background="#ffffff").pack(side="left")
        PillButton(actions, "导出原文和译文", export, width=160, fill="#dce6fb", background="#ffffff").pack(side="right")

    def load_session(self, sid):
        self.session_id = sid
        rows = self.history_store.segments(sid)
        self.session_label.configure(text=f"会话 #{sid}")
        if rows:
            self.source.configure(text=rows[-1][2])
            self.target.configure(text=rows[-1][3])
        self.status.set(f"已选择历史会话 #{sid}，点击开始后继续记录")

    def start_new_session(self):
        if self.worker and self.worker.is_alive():
            messagebox.showinfo("请先暂停", "先暂停当前录音，再新建会话。", parent=self)
            return
        self.session_id = None
        self.session_label.configure(text="新会话")
        self.source.configure(text="原文会显示在这里")
        self.target.configure(text="翻译会显示在这里")
        self.status.set("下一次开始将创建新会话")

    def settings(self):
        page = self._open_page("settings")
        header = tk.Frame(page, bg="#f8faff")
        header.pack(fill="x", padx=24, pady=(20, 24))
        PillButton(header, "← 返回", self.back_to_subtitles, width=90, fill="#dce6fb", background="#f8faff").pack(side="left")
        tk.Label(header, text="服务设置", bg="#f8faff", fg="#3a3360", font=("Microsoft YaHei UI", 19, "bold")).pack(side="left", padx=(17, 0))
        form = tk.Frame(page, bg="#ffffff", highlightbackground="#dfe7fb", highlightthickness=1)
        form.pack(fill="x", padx=80, pady=(0, 20))
        fields = [("DeepSeek API Key", "deepseek_key"), ("腾讯云 AppID", "tencent_appid"), ("腾讯云 SecretId", "tencent_id"), ("腾讯云 SecretKey", "tencent_key")]
        entries = {}
        for row, (label, account) in enumerate(fields):
            tk.Label(form, text=label, bg="#ffffff", fg="#536a9b").grid(row=row, column=0, padx=(25, 18), pady=16, sticky="w")
            entry = ttk.Entry(form, width=55, show="*" if "Key" in label or "Secret" in label else "")
            entry.grid(row=row, column=1, padx=(0, 25), pady=16, sticky="ew")
            entry.insert(0, keyring.get_password(SERVICE, account) or "")
            entries[account] = entry
        form.grid_columnconfigure(1, weight=1)
        def save():
            for account, entry in entries.items():
                value = entry.get().strip()
                if value:
                    keyring.set_password(SERVICE, account, value)
                else:
                    try: keyring.delete_password(SERVICE, account)
                    except keyring.errors.PasswordDeleteError: pass
            self.back_to_subtitles()
        PillButton(page, "保存设置", save, width=120, fill="#c8d3f6", background="#f8faff").pack(anchor="e", padx=80)

    def toggle(self):
        if self.worker and self.worker.is_alive():
            self.stop_event.set()
            if self.stream: self.stream.stop(); self.stream.close(); self.stream = None
            if self.session_id is not None: self.history_store.end(self.session_id)
            self.start_button.set_label("● 开始", "#c8d3f6")
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
        self.start_button.set_label("Ⅱ 暂停", "#f3d399")
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
            keys = getattr(self, "route_keys", None)
            creds = [keys[x] if keys is not None else keyring.get_password(SERVICE, x)
                     for x in ("tencent_appid", "tencent_id", "tencent_key")]
            ws = websocket.create_connection(tencent_url(*creds), timeout=10)
            ws.settimeout(1)
            finished = threading.Event()
            def receive():
                while not finished.is_set():
                    try: message = ws.recv()
                    except websocket.WebSocketTimeoutException: continue
                    except Exception:
                        finished.set()
                        break
                    if not isinstance(message, str): continue
                    data = json.loads(message)
                    if data.get("code", 0) != 0:
                        self.events.put(("status", f"腾讯云错误：{data.get('message', data['code'])}"))
                        finished.set()
                        self.stop_event.set()
                        break
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
            while not finished.is_set() and (not self.stop_event.is_set() or not self.audio.empty()):
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
                    if event[0] == "final_pair" and event[5] is not None and (event[3] or event[4]):
                        self.history_store.append(event[5], event[1], event[2], event[3], event[4])
                        if self.active_view == "history":
                            self.history_refresh()
                elif event[0] == "source":
                    self.source.configure(text=event[1]); self.target.configure(text="翻译中…")
                elif event[0] == "status": self.status.set(event[1])
                elif event[0] == "stopped":
                    if self.stream: self.stream.stop(); self.stream.close(); self.stream = None
                    if self.session_id is not None: self.history_store.end(self.session_id)
                    self.start_button.set_label("● 开始", "#c8d3f6")
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
