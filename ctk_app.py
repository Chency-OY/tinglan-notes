"""One-window, ice-blue desktop UI for live Chinese-English interpretation."""
from __future__ import annotations

import queue
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox

import customtkinter as ctk
import keyring
import sounddevice as sd
from PIL import Image

from interpreter import HistoryStore, Interpreter as AudioBackend, SERVICE, clock_text, SAMPLE_RATE, FRAME_SAMPLES


BG = "#F8FBFF"
INK = "#35466F"
MUTED = "#8191B4"
BLUE = "#6F85BC"
PALE = "#EDF3FF"
ICE = "#E3ECFF"
HOVER = "#E5EDFC"
FONT = "Microsoft YaHei UI"


def ui_button(master, text, command, *, primary=False, width=92):
    return ctk.CTkButton(
        master, text=text, command=command, width=width, height=35, corner_radius=9,
        fg_color=BLUE if primary else "transparent",
        hover_color="#5E77B3" if primary else HOVER,
        text_color="#FFFFFF" if primary else "#526B9B",
        font=ctk.CTkFont(family=FONT, size=12, weight="bold" if primary else "normal"),
        border_width=0,
    )


class InterpreterWindow(ctk.CTk):
    capture = AudioBackend.capture
    deepseek_loop = AudioBackend.deepseek_loop
    tencent_loop = AudioBackend.tencent_loop

    def __init__(self):
        ctk.set_appearance_mode("light")
        super().__init__(fg_color=BG)
        self.title("中英同传字幕")
        icon_dir = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))
        if (icon_dir / "icon.ico").exists():
            self.iconbitmap(str(icon_dir / "icon.ico"))
        self.geometry("760x355+300+100")
        self.minsize(560, 290)
        self.attributes("-topmost", True)
        self.opacity = .88
        self.attributes("-alpha", self.opacity)
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.zoom = 1.0
        self.active_view = "subtitles"
        self.history_store = HistoryStore()
        self.session_id: int | None = None
        self.capture_started = 0.0
        self.events: queue.Queue[tuple] = queue.Queue()
        self.audio: queue.Queue[bytes] = queue.Queue(maxsize=100)
        self.stop_event = threading.Event()
        self.worker: threading.Thread | None = None
        self.stream = None
        self.closing = False
        self.devices = {f"{i}: {d['name']}": i for i, d in enumerate(sd.query_devices()) if d["max_input_channels"] > 0}
        self.mode = ctk.StringVar(value="DeepSeek 近实时")
        self.device = ctk.StringVar(value=next(iter(self.devices), "无可用麦克风"))
        self.status = ctk.StringVar(value="未启动")
        self.icon_image = None
        if (icon_dir / "icon.png").exists():
            self.icon_image = ctk.CTkImage(Image.open(icon_dir / "icon.png"), size=(34, 34))
        self._build_compact()
        self.bind("<Configure>", self.on_resize)
        self.bind("<Control-MouseWheel>", self.on_zoom)
        self.after(80, self.poll)

    def _build_compact(self):
        self.compact = ctk.CTkFrame(self, fg_color=BG, corner_radius=0)
        self.compact.pack(fill="both", expand=True)
        header = ctk.CTkFrame(self.compact, fg_color="transparent")
        header.pack(fill="x", padx=20, pady=(16, 11))
        if self.icon_image:
            ctk.CTkLabel(header, text="", image=self.icon_image, width=36).pack(side="left", padx=(0, 8))
        ctk.CTkLabel(header, text="中英同传", text_color=INK, font=ctk.CTkFont(family=FONT, size=19, weight="bold")).pack(side="left")
        self.session_label = ctk.CTkLabel(header, text="新会话", text_color=MUTED, font=ctk.CTkFont(family=FONT, size=11))
        self.session_label.pack(side="left", padx=(10, 0), pady=(4, 0))
        ui_button(header, "☰  历史", self.show_history, width=82).pack(side="right")
        ui_button(header, "设置", self.show_settings, width=58).pack(side="right", padx=(0, 4))

        controls = ctk.CTkFrame(self.compact, fg_color="transparent")
        controls.pack(fill="x", padx=20, pady=(0, 11))
        combo = dict(height=37, corner_radius=9, fg_color="#FFFFFF", border_color="#D9E4F7",
                     button_color="#E8F0FF", button_hover_color="#DAE7FC", text_color=INK,
                     dropdown_fg_color="#FFFFFF", dropdown_hover_color=HOVER,
                     font=ctk.CTkFont(family=FONT, size=12))
        self.mode_combo = ctk.CTkComboBox(controls, values=["DeepSeek 近实时", "腾讯云 实时"], variable=self.mode, width=170, **combo)
        self.mode_combo.pack(side="left", padx=(0, 8))
        self.device_combo = ctk.CTkComboBox(controls, values=list(self.devices) or ["无可用麦克风"], variable=self.device, **combo)
        self.device_combo.pack(side="left", fill="x", expand=True)
        self.start_button = ui_button(controls, "●  开始", self.toggle, primary=True, width=95)
        self.start_button.pack(side="right", padx=(9, 0))

        source_card = ctk.CTkFrame(self.compact, fg_color=PALE, corner_radius=14)
        source_card.pack(fill="x", padx=20, pady=(0, 9))
        ctk.CTkLabel(source_card, text="原文  ORIGINAL", text_color="#768DB8", font=ctk.CTkFont(family=FONT, size=11, weight="bold"), anchor="w").pack(fill="x", padx=16, pady=(10, 0))
        self.source = ctk.CTkLabel(source_card, text="说话后，原文会显示在这里", text_color="#5C7097", font=ctk.CTkFont(family=FONT, size=13), anchor="w", justify="left")
        self.source.pack(fill="x", padx=16, pady=(1, 12))
        target_card = ctk.CTkFrame(self.compact, fg_color=ICE, corner_radius=14)
        target_card.pack(fill="both", expand=True, padx=20, pady=(0, 8))
        ctk.CTkLabel(target_card, text="译文  TRANSLATION", text_color="#6F83B0", font=ctk.CTkFont(family=FONT, size=11, weight="bold"), anchor="w").pack(fill="x", padx=16, pady=(11, 0))
        self.target = ctk.CTkLabel(target_card, text="翻译结果会显示在这里", text_color=INK, font=ctk.CTkFont(family=FONT, size=20, weight="bold"), anchor="w", justify="left")
        self.target.pack(fill="both", expand=True, padx=16, pady=(2, 12))
        footer = ctk.CTkFrame(self.compact, fg_color="transparent")
        footer.pack(fill="x", padx=22, pady=(0, 12))
        ctk.CTkLabel(footer, textvariable=self.status, text_color=MUTED, font=ctk.CTkFont(family=FONT, size=11)).pack(side="left")
        ctk.CTkSlider(footer, from_=0.55, to=1.0, width=105, height=14, fg_color="#D9E4F8", progress_color="#A7B9E0", button_color="#8DA3D3", button_hover_color="#7D95C9", command=self.set_opacity).pack(side="right")
        ctk.CTkLabel(footer, text="透明度", text_color=MUTED, font=ctk.CTkFont(family=FONT, size=11)).pack(side="right", padx=(0, 8))

    def set_opacity(self, value):
        self.opacity = float(value)
        if self.active_view == "subtitles":
            self.attributes("-alpha", self.opacity)

    def on_zoom(self, event):
        if self.active_view == "subtitles":
            self.zoom = min(1.8, max(.7, self.zoom + (.1 if event.delta > 0 else -.1)))
            self.on_resize()

    def on_resize(self, event=None):
        if self.active_view != "subtitles" or not hasattr(self, "source"):
            return
        factor = min(self.winfo_width() / 760, self.winfo_height() / 355) * self.zoom
        self.source.configure(font=ctk.CTkFont(family=FONT, size=max(11, min(23, round(13 * factor)))), wraplength=max(400, self.winfo_width() - 74))
        self.target.configure(font=ctk.CTkFont(family=FONT, size=max(16, min(36, round(20 * factor))), weight="bold"), wraplength=max(400, self.winfo_width() - 74))

    def open_view(self, name):
        self.compact_geometry = self.geometry()
        self.compact.pack_forget()
        self.active_view = name
        self.attributes("-topmost", False)
        self.attributes("-alpha", 1.0)
        self.minsize(760, 520)
        self.geometry("1060x700+180+70")
        self.page = ctk.CTkFrame(self, fg_color=BG, corner_radius=0)
        self.page.pack(fill="both", expand=True)
        return self.page

    def back(self):
        self.page.destroy()
        self.active_view = "subtitles"
        self.minsize(560, 290)
        self.geometry(self.compact_geometry)
        self.compact.pack(fill="both", expand=True)
        self.attributes("-alpha", self.opacity)
        self.attributes("-topmost", True)
        self.on_resize()

    def show_history(self):
        page = self.open_view("history")
        sidebar = ctk.CTkFrame(page, fg_color="#F0F5FF", corner_radius=0, width=270)
        sidebar.pack(side="left", fill="y")
        sidebar.pack_propagate(False)
        ui_button(sidebar, "←  返回字幕", self.back, width=110).pack(anchor="w", padx=15, pady=(20, 17))
        ctk.CTkLabel(sidebar, text="历史对话", text_color=INK, font=ctk.CTkFont(family=FONT, size=21, weight="bold"), anchor="w").pack(fill="x", padx=23)
        ui_button(sidebar, "＋  新会话", self.new_session_from_history, width=225).pack(padx=18, pady=(18, 18), fill="x")
        ctk.CTkLabel(sidebar, text="最近", text_color=MUTED, font=ctk.CTkFont(family=FONT, size=11, weight="bold"), anchor="w").pack(fill="x", padx=23, pady=(0, 8))
        self.session_scroll = ctk.CTkScrollableFrame(sidebar, fg_color="transparent", scrollbar_button_color="#C8D7F0", scrollbar_button_hover_color="#AABEE4")
        self.session_scroll.pack(fill="both", expand=True, padx=(10, 5), pady=(0, 12))
        self.session_buttons = {}
        sessions = self.history_store.sessions()
        for sid, started, ended, provider in sessions:
            text = f"{datetime.fromtimestamp(started):%m月%d日  %H:%M}\n{provider}"
            button = ctk.CTkButton(self.session_scroll, text=text, height=62, corner_radius=10, anchor="w", fg_color="transparent", hover_color=HOVER, text_color="#52678F", font=ctk.CTkFont(family=FONT, size=12, weight="normal"), command=lambda session_id=sid: self.select_session(session_id))
            button.pack(fill="x", padx=4, pady=2)
            self.session_buttons[sid] = button

        content = ctk.CTkFrame(page, fg_color=BG, corner_radius=0)
        content.pack(side="left", fill="both", expand=True, padx=(30, 34), pady=(24, 24))
        self.history_heading = ctk.CTkLabel(content, text="选择一段对话", text_color=INK, font=ctk.CTkFont(family=FONT, size=23, weight="bold"), anchor="w")
        self.history_heading.pack(fill="x")
        self.history_meta = ctk.CTkLabel(content, text="整场原文和译文会在这里按时间排列", text_color=MUTED, font=ctk.CTkFont(family=FONT, size=11), anchor="w")
        self.history_meta.pack(fill="x", pady=(2, 12))
        self.messages = ctk.CTkScrollableFrame(content, fg_color="#FFFFFF", corner_radius=14, scrollbar_button_color="#C8D7F0", scrollbar_button_hover_color="#AABEE4")
        self.messages.pack(fill="both", expand=True)
        actions = ctk.CTkFrame(content, fg_color="transparent")
        actions.pack(fill="x", pady=(14, 0))
        ui_button(actions, "继续这段对话", self.continue_selected, width=125).pack(side="left")
        ui_button(actions, "导出原文和译文", self.export_selected, width=140).pack(side="right")
        self.selected_session_id = None
        if sessions:
            self.select_session(self.session_id if self.session_id in self.session_buttons else sessions[0][0])

    def select_session(self, sid):
        self.selected_session_id = sid
        for session_id, button in self.session_buttons.items():
            button.configure(fg_color="#DDE8FC" if session_id == sid else "transparent", text_color=INK if session_id == sid else "#52678F")
        session = next(row for row in self.history_store.sessions() if row[0] == sid)
        self.history_heading.configure(text=f"会话 · {datetime.fromtimestamp(session[1]):%m月%d日 %H:%M}")
        self.history_meta.configure(text=f"开始 {clock_text(session[1])}    结束 {clock_text(session[2]) if session[2] else '进行中'}")
        for child in self.messages.winfo_children():
            child.destroy()
        for started, ended, original, translated in self.history_store.segments(sid):
            ctk.CTkLabel(self.messages, text=f"{clock_text(started)}  →  {clock_text(ended)}", text_color="#A0ADC8", font=ctk.CTkFont(family=FONT, size=10)).pack(pady=(16, 7))
            for color, title, text, pad in ((PALE, "原文", original, (12, 100)), (ICE, "译文", translated, (100, 12))):
                card = ctk.CTkFrame(self.messages, fg_color=color, corner_radius=13)
                card.pack(fill="x", padx=pad, pady=(0, 6))
                ctk.CTkLabel(card, text=title, text_color="#7287B0", font=ctk.CTkFont(family=FONT, size=10, weight="bold"), anchor="w").pack(fill="x", padx=16, pady=(11, 0))
                ctk.CTkLabel(card, text=text, text_color=INK, font=ctk.CTkFont(family=FONT, size=13), anchor="w", justify="left", wraplength=460).pack(fill="x", padx=16, pady=(3, 13))

    def new_session_from_history(self):
        if self.worker and self.worker.is_alive():
            messagebox.showinfo("请先暂停", "先暂停录音，再新建会话。", parent=self)
            return
        self.session_id = None
        self.session_label.configure(text="新会话")
        self.source.configure(text="说话后，原文会显示在这里")
        self.target.configure(text="翻译结果会显示在这里")
        self.status.set("下一次开始将创建新会话")
        self.back()

    def continue_selected(self):
        sid = self.selected_session_id
        if sid is None:
            return
        if self.worker and self.worker.is_alive():
            messagebox.showinfo("请先暂停", "先暂停录音，再继续历史会话。", parent=self)
            return
        self.session_id = sid
        rows = self.history_store.segments(sid)
        self.session_label.configure(text=f"会话 #{sid}")
        if rows:
            self.source.configure(text=rows[-1][2])
            self.target.configure(text=rows[-1][3])
        self.status.set(f"已选择会话 #{sid}，点击开始后继续记录")
        self.back()

    def export_selected(self):
        sid = self.selected_session_id
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

    def show_settings(self):
        page = self.open_view("settings")
        ui_button(page, "←  返回字幕", self.back, width=110).pack(anchor="w", padx=30, pady=(22, 18))
        ctk.CTkLabel(page, text="服务设置", text_color=INK, font=ctk.CTkFont(family=FONT, size=23, weight="bold"), anchor="w").pack(fill="x", padx=45)
        ctk.CTkLabel(page, text="密钥只保存在当前 Windows 用户的凭据管理器中。", text_color=MUTED, font=ctk.CTkFont(family=FONT, size=11), anchor="w").pack(fill="x", padx=45, pady=(3, 18))
        form = ctk.CTkFrame(page, fg_color="#FFFFFF", corner_radius=14)
        form.pack(fill="x", padx=45)
        self.key_entries = {}
        for label, account in (("DeepSeek API Key", "deepseek_key"), ("腾讯云 AppID", "tencent_appid"), ("腾讯云 SecretId", "tencent_id"), ("腾讯云 SecretKey", "tencent_key")):
            ctk.CTkLabel(form, text=label, text_color="#647AA7", font=ctk.CTkFont(family=FONT, size=11), anchor="w").pack(fill="x", padx=22, pady=(14, 3))
            entry = ctk.CTkEntry(form, height=38, corner_radius=9, fg_color=BG, border_color="#D9E4F7", text_color=INK, show="*" if "key" in account or account == "tencent_id" else "")
            entry.pack(fill="x", padx=22, pady=(0, 4))
            try:
                entry.insert(0, keyring.get_password(SERVICE, account) or "")
            except Exception:
                entry.configure(placeholder_text="无法读取已保存的密钥")
            self.key_entries[account] = entry
        ctk.CTkFrame(form, height=10, fg_color="transparent").pack()
        ui_button(page, "保存设置", self.save_settings, primary=True, width=110).pack(anchor="e", padx=45, pady=20)

    def save_settings(self):
        try:
            for account, entry in self.key_entries.items():
                value = entry.get().strip()
                if value:
                    keyring.set_password(SERVICE, account, value)
                else:
                    try: keyring.delete_password(SERVICE, account)
                    except keyring.errors.PasswordDeleteError: pass
        except Exception as exc:
            messagebox.showerror("保存失败", str(exc), parent=self)
            return
        self.back()

    def toggle(self):
        if self.worker and self.worker.is_alive():
            self.stop_event.set()
            if self.stream: self.stream.stop(); self.stream.close(); self.stream = None
            if self.session_id is not None: self.history_store.end(self.session_id)
            self.start_button.configure(text="●  开始")
            self.mode_combo.configure(state="normal")
            self.device_combo.configure(state="normal")
            self.status.set("正在停止")
            return
        credentials = ["deepseek_key"] if self.mode.get().startswith("DeepSeek") else ["tencent_appid", "tencent_id", "tencent_key"]
        try:
            missing = any(not keyring.get_password(SERVICE, key) for key in credentials)
        except Exception as exc:
            messagebox.showerror("凭据读取失败", str(exc), parent=self)
            return
        if missing:
            messagebox.showinfo("需要密钥", "请先在“设置”中填写当前服务的凭据。", parent=self)
            return
        if self.device.get() not in self.devices:
            messagebox.showerror("麦克风", "没有可用的麦克风。", parent=self)
            return
        self.stop_event.clear()
        self.audio = queue.Queue(maxsize=100)
        try:
            self.stream = sd.RawInputStream(samplerate=SAMPLE_RATE, channels=1, dtype="int16", blocksize=FRAME_SAMPLES, device=self.devices[self.device.get()], callback=self.capture)
            self.stream.start()
        except Exception as exc:
            self.stream = None
            messagebox.showerror("麦克风启动失败", str(exc), parent=self)
            return
        self.worker = threading.Thread(target=self.deepseek_loop if self.mode.get().startswith("DeepSeek") else self.tencent_loop, daemon=True)
        self.capture_started = time.time()
        if self.session_id is None:
            self.session_id = self.history_store.begin(self.mode.get())
            self.session_label.configure(text=f"会话 #{self.session_id}")
        else:
            self.history_store.resume(self.session_id)
        self.worker.start()
        self.start_button.configure(text="Ⅱ  暂停")
        self.mode_combo.configure(state="disabled")
        self.device_combo.configure(state="disabled")
        self.status.set("正在收音")

    def poll(self):
        try:
            while True:
                event = self.events.get_nowait()
                if event[0] in ("pair", "final_pair"):
                    self.source.configure(text=event[3]); self.target.configure(text=event[4])
                    if event[0] == "final_pair" and event[5] is not None and (event[3] or event[4]):
                        self.history_store.append(event[5], event[1], event[2], event[3], event[4])
                        if self.active_view == "history" and self.selected_session_id == event[5]:
                            self.select_session(event[5])
                elif event[0] == "source":
                    self.source.configure(text=event[1]); self.target.configure(text="翻译中…")
                elif event[0] == "status": self.status.set(event[1])
                elif event[0] == "stopped":
                    if self.stream: self.stream.stop(); self.stream.close(); self.stream = None
                    if self.session_id is not None: self.history_store.end(self.session_id)
                    self.start_button.configure(text="●  开始")
                    self.mode_combo.configure(state="normal")
                    self.device_combo.configure(state="normal")
        except queue.Empty: pass
        if self.closing and (not self.worker or not self.worker.is_alive()) and self.events.empty():
            if self.session_id is not None: self.history_store.end(self.session_id)
            self.destroy()
        else:
            self.after(80, self.poll)

    def close(self):
        if self.closing:
            return
        self.closing = True
        self.stop_event.set()
        if self.stream: self.stream.stop(); self.stream.close(); self.stream = None
        self.status.set("正在保存最后一段…")
        if not self.worker or not self.worker.is_alive():
            if self.session_id is not None: self.history_store.end(self.session_id)
            self.destroy()


if __name__ == "__main__":
    InterpreterWindow().mainloop()
