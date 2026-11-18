"""Single-window conversation workspace for the Windows interpreter."""
from __future__ import annotations

import queue
import sys
import threading
import time
import tkinter as tk
from datetime import datetime
from pathlib import Path

import customtkinter as ctk
import keyring
import sounddevice as sd
from PIL import Image, ImageDraw, ImageOps

from interpreter import FRAME_SAMPLES, SAMPLE_RATE, SERVICE, HistoryStore, Interpreter as AudioBackend, clock_text

PAPER = "#FAFBFE"
SIDE = "#EEF2FB"
INK = "#303B59"
SUB = "#596884"
MUTED = "#657490"
ACCENT = "#6278AC"
SELECT = "#DDE6F8"
BORDER = "#DEE5F2"
FONT = "Microsoft YaHei UI"
ASSETS = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))


def font(size, bold=False, family=FONT):
    return ctk.CTkFont(family=family, size=size, weight="bold" if bold else "normal")


def label(parent, text, size=12, color=INK, bold=False, **kwargs):
    return ctk.CTkLabel(parent, text=text, height=0, font=font(size, bold), text_color=color, **kwargs)


def button(parent, text, command, width=32, *, symbol=False, fill="transparent", anchor="center"):
    return ctk.CTkButton(parent, text=text, command=command, width=width, height=32,
                         fg_color=fill, hover_color=SELECT, text_color=SUB, corner_radius=12,
                         anchor=anchor, font=font(16 if symbol else 12, family="Segoe MDL2 Assets" if symbol else FONT))


def orb_image(portrait, recording):
    # Supersample the portrait, surrounding ring and small play/pause badge as one button.
    size = 240
    im = Image.new("RGBA", (size, size))
    draw = ImageDraw.Draw(im)
    draw.ellipse((5, 8, 235, 238), fill="#DDE5F7")
    draw.ellipse((6, 2, 234, 230), fill="#FFFFFF", outline="#8599CA" if recording else "#C7D4EF", width=6)
    draw.ellipse((18, 14, 222, 218), fill="#E5EBFF")
    face = ImageOps.contain(portrait, (184, 184), Image.Resampling.LANCZOS)
    im.alpha_composite(face, ((size - face.width) // 2, 22))
    draw.ellipse((158, 151, 234, 227), fill="#AD637D" if recording else ACCENT, outline="#FFFFFF", width=7)
    if recording:
        for x in (183, 199):
            draw.rounded_rectangle((x, 176, x + 8, 202), radius=3, fill="white")
    else:
        draw.polygon([(188, 175), (188, 204), (209, 190)], fill="white")
    return ctk.CTkImage(im, size=(60, 60))


def wave_image():
    im = Image.new("RGBA", (360, 128))
    draw = ImageDraw.Draw(im)
    heights = (8, 16, 31, 50, 80, 48, 27, 55, 96, 67, 34, 19, 8)
    for i, height in enumerate(heights):
        x = 30 + 24 * i
        color = ("#BACCF1", "#94B1E3", "#96A7DB", "#AEA6D6")[min(i // 4, 3)]
        draw.rounded_rectangle((x, (128-height)//2, x+12, (128+height)//2), radius=6, fill=color)
    return ctk.CTkImage(im, size=(90, 32))


class InterpreterWindow(ctk.CTk):
    capture = AudioBackend.capture

    def __init__(self, history_store=None):
        ctk.set_appearance_mode("light")
        super().__init__(fg_color=PAPER)
        self.title("中英同传字幕")
        self.iconbitmap(str(ASSETS / "icon.ico"))
        self.geometry("800x460")
        self.minsize(540, 300)
        self.opacity = .94
        self.attributes("-topmost", True)
        self.attributes("-alpha", self.opacity)
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.history_store = history_store or HistoryStore()
        self.session_id = self.view_id = None
        self.recording = self.closing = False
        self.panel = None
        self.before_settings = None
        self.export_folder = ctk.StringVar(value=str(Path.home() / "Documents" / "同传记录"))
        self.zoom = 1.
        self.worker = self.stream = None
        self.capture_started = 0.
        self.stop_event = threading.Event()
        self.events = queue.Queue()
        self.audio = queue.Queue(maxsize=100)
        self.partial = ("", "")
        self.titled_sessions = set()
        self.devices = {f"{i} · {d['name']}": i for i, d in enumerate(sd.query_devices()) if d["max_input_channels"] > 0}
        self.device = ctk.StringVar(value=next(iter(self.devices), "无可用麦克风"))
        self.mode = ctk.StringVar(value="DeepSeek 近实时")
        self.status = ctk.StringVar(value="")
        portrait = Image.open(ASSETS / "icon.png").convert("RGBA")
        self.orbs = [orb_image(portrait, active) for active in (False, True)]
        self.wave = wave_image()
        self.grid_rowconfigure(0, weight=1)
        self.grid_columnconfigure(1, weight=1)
        self.side = ctk.CTkFrame(self, width=204, corner_radius=0, fg_color=SIDE)
        self.side.pack_propagate(False)
        self.main = ctk.CTkFrame(self, corner_radius=0, fg_color=PAPER)
        self.main.grid(row=0, column=1, sticky="nsew")
        self.main.grid_columnconfigure(0, weight=1)
        self.main.grid_rowconfigure(1, weight=1)
        self._build_header()
        self._build_feed()
        self._build_dock()
        self._select(None)
        self.bind("<Control-MouseWheel>", self._zoom)
        self.bind("<Escape>", lambda _: self.set_panel(None))
        self.bind("<Control-b>", lambda _: self.toggle_history())
        self.after(80, self.poll)

    def _build_header(self):
        bar = ctk.CTkFrame(self.main, fg_color="transparent")
        bar.grid(row=0, column=0, sticky="ew", padx=14, pady=(12, 0))
        self.history_toggle = button(bar, "历史", self.toggle_history, 50, fill="#EEF2FA")
        self.history_toggle.pack(side="left", padx=(0, 10))
        title_box = ctk.CTkFrame(bar, fg_color="transparent")
        title_box.pack(side="left", fill="x", expand=True)
        self.heading = label(title_box, "", 16, bold=True, anchor="w")
        self.heading.pack(fill="x")
        self.meta = label(title_box, "", 10, MUTED, anchor="w")
        self.meta.pack(fill="x")
        self.settings_btn = button(bar, "设置", self.toggle_settings, 44)
        self.settings_btn.pack(side="right")
        self.export_btn = button(bar, "导出", lambda: self.set_panel(None if self.panel == "export" else "export"), 44)
        self.export_btn.pack(side="right")
        self.new_btn = button(bar, "\ue710", self.new_session, symbol=True)
        self.new_btn.pack(side="right")

    def _build_feed(self):
        self.body = ctk.CTkFrame(self.main, fg_color="transparent")
        self.body.grid(row=1, column=0, sticky="nsew", padx=16, pady=(5, 4))
        self.empty = ctk.CTkFrame(self.body, fg_color="transparent")
        ctk.CTkLabel(self.empty, text="", image=self.wave, height=32).pack()
        self.empty_title = label(self.empty, "", 23, bold=True)
        self.empty_title.pack(pady=(8, 4))
        self.empty_hint = label(self.empty, "", color=MUTED)
        self.empty_hint.pack()
        self.transcript = tk.Text(self.body, bg=PAPER, fg=INK, relief="flat", borderwidth=0,
                                  highlightthickness=0, wrap="word", padx=12, pady=8, cursor="arrow",
                                  selectbackground=SELECT, state="disabled", width=1, height=1)
        self.scroll = ctk.CTkScrollbar(self.body, width=9, button_color=BORDER, button_hover_color="#BCCAE4",
                                       command=self.transcript.yview)
        self.transcript.configure(yscrollcommand=self.scroll.set)
        self._size_fonts()
        self.body.bind("<Configure>", lambda _: self._size_fonts())

    def _size_fonts(self):
        box = self.transcript
        factor = min(1.1, max(.85, self.body.winfo_width() / self.body._get_widget_scaling() / 620))
        for tag, size, color, before, after, weight in (
            ("stamp", 9, MUTED, 10, 3, "normal"), ("original", 12, SUB, 0, 4, "normal"),
            ("translation", 16, INK, 0, 12, "bold")):
            box.tag_configure(tag, font=(FONT, max(8, round(size * factor * self.zoom)), weight),
                              foreground=color, spacing1=before, spacing3=after)

    def _zoom(self, event):
        self.zoom = min(1.8, max(.7, self.zoom + (.1 if event.delta > 0 else -.1)))
        self._size_fonts()
        return "break"

    def _build_dock(self):
        dock = ctk.CTkFrame(self.main, fg_color="transparent")
        dock.grid(row=2, column=0, sticky="ew", padx=16, pady=(0, 13))
        dock.grid_columnconfigure(0, weight=1)
        self.notice = label(dock, "", 11, ACCENT, anchor="w", textvariable=self.status)
        self.notice.grid(row=0, column=0, columnspan=2, sticky="ew", padx=8, pady=(0, 3))
        strip = ctk.CTkFrame(dock, fg_color="transparent")
        strip.grid(row=1, column=0, sticky="ew", padx=(4, 6))
        badge = ctk.CTkFrame(strip, fg_color="#EAF0FC", corner_radius=12)
        badge.pack(side="left", padx=(0, 8))
        label(badge, "中  ⇄  EN", 12, ACCENT, bold=True).pack(padx=10, pady=6)
        self.action = label(strip, "", 12, SUB, anchor="w")
        self.action.pack(side="left", fill="x", expand=True, padx=(0, 5))
        self.live_link = button(strip, "回到录制", lambda: self.select_session(self.session_id), 68, fill=SELECT)
        self.orb = ctk.CTkButton(dock, text="", image=self.orbs[0], width=66, height=66, corner_radius=6,
                                 border_spacing=0, fg_color="transparent", hover_color="#E7ECF9", command=self.toggle)
        self.orb.grid(row=1, column=1)
        tk.Frame.configure(self.orb, takefocus=True)
        tk.Misc.bind(self.orb, "<Return>", lambda _: self.toggle())
        tk.Misc.bind(self.orb, "<space>", lambda _: self.toggle())
        tk.Misc.bind(self.orb, "<FocusIn>", lambda _: self.orb.configure(fg_color=SELECT))
        tk.Misc.bind(self.orb, "<FocusOut>", lambda _: self.orb.configure(fg_color="transparent"))

    def _dock_state(self):
        stopping = self.recording and self.stop_event.is_set()
        text = "正在保存…" if stopping else "点击头像暂停" if self.recording else "点击头像继续" if self.view_id else "点击头像录制"
        self.action.configure(text=text)
        self.orb.configure(image=self.orbs[int(self.recording)], state="disabled" if stopping else "normal")
        if self.recording and self.view_id != self.session_id:
            self.action.pack_forget()
            self.live_link.pack(side="left")
        else:
            self.live_link.pack_forget()
            if not self.action.winfo_manager():
                self.action.pack(side="left", fill="x", expand=True)

    def toggle_history(self):
        self.set_panel(None if self.panel == "history" else "history")

    def toggle_settings(self):
        if self.panel == "settings":
            self.set_panel(self.before_settings)
        else:
            self.before_settings = self.panel
            self.set_panel("settings")

    def set_panel(self, name):
        self.panel = name
        for child in self.side.winfo_children():
            child.destroy()
        self.history_toggle.configure(fg_color=SELECT if name == "history" else "#EEF2FA")
        self.settings_btn.configure(fg_color=SELECT if name == "settings" else "transparent")
        if name is None:
            self.side.grid_remove()
            return
        self.side.grid(row=0, column=0, sticky="ns")
        row = ctk.CTkFrame(self.side, fg_color="transparent")
        row.pack(fill="x", padx=(17, 9), pady=(12, 6))
        label(row, {"history": "历史对话", "settings": "录制设置", "export": "导出对话"}[name], 15, bold=True).pack(side="left")
        button(row, "\ue76b", self.toggle_settings if name == "settings" else lambda: self.set_panel(None), symbol=True).pack(side="right")
        if name == "settings":
            self._settings()
        elif name == "export":
            label(self.side, "保存位置", 12, SUB).pack(anchor="w", padx=16, pady=(18, 6))
            entry = ctk.CTkEntry(self.side, textvariable=self.export_folder, border_color=BORDER,
                                 border_width=1, fg_color="white", text_color=INK)
            entry.pack(fill="x", padx=12)
            label(self.side, "原文、译文分别保存为 TXT，\n包含会话和每段字幕的时间。", 11, MUTED,
                  justify="left").pack(anchor="w", padx=16, pady=12)
            button(self.side, "保存两个文件", self.export, fill=SELECT).pack(fill="x", padx=12)
        else:
            button(self.side, "＋   新会话", self.new_session, fill="#E1E9F9", anchor="w").pack(fill="x", padx=12, pady=(6, 9))
            self.listing = ctk.CTkScrollableFrame(self.side, fg_color="transparent", scrollbar_button_color=BORDER)
            self.listing.pack(fill="both", expand=True, padx=(8, 2), pady=(0, 8))
            self._history_list()

    def _history_list(self):
        if self.panel != "history":
            return
        for child in self.listing.winfo_children():
            child.destroy()
        group = None
        rows = self.history_store.session_previews()
        if not rows:
            label(self.listing, "录制后，会话会出现在这里", 11, MUTED).pack(anchor="w", padx=6, pady=6)
        for sid, started, preview in rows:
            date = datetime.fromtimestamp(started)
            day = "今天" if date.date() == datetime.now().date() else date.strftime("%Y/%m/%d")
            if day != group:
                label(self.listing, day, 11, MUTED).pack(anchor="w", padx=8, pady=(12, 4))
                group = day
            title = " ".join((preview or "同传会话").split())
            text = f"{date:%H:%M}   {title[:12]}" + ("…" if len(title) > 12 else "")
            button(self.listing, text, lambda s=sid: self.select_session(s), fill=SELECT if sid == self.view_id else "transparent",
                   anchor="w").pack(fill="x", pady=1)

    def _settings(self):
        save = button(self.side, "保存设置", self.save_settings, fill=SELECT)
        save.pack(side="bottom", fill="x", padx=14, pady=10)
        form = ctk.CTkScrollableFrame(self.side, fg_color="transparent", scrollbar_button_color=BORDER)
        form.pack(fill="both", expand=True, padx=(8, 2))
        def caption(text):
            label(form, text, 11, SUB).pack(anchor="w", padx=4, pady=(12, 4))
        self.options = []
        for name, var, values in (("翻译服务", self.mode, ["DeepSeek 近实时", "腾讯云 实时"]),
                                   ("麦克风", self.device, list(self.devices) or ["无可用麦克风"])):
            caption(name)
            menu = ctk.CTkOptionMenu(form, variable=var, values=values, width=110, dynamic_resizing=False,
                                     fg_color="white", button_color="#DCE5F6", button_hover_color=SELECT,
                                     text_color=INK, dropdown_fg_color=PAPER, dropdown_text_color=INK,
                                     state="disabled" if self.recording else "normal", font=font(11))
            menu.pack(fill="x", padx=2)
            self.options.append(menu)
        caption("窗口透明度")
        slider = ctk.CTkSlider(form, from_=.55, to=1., width=100, button_color=ACCENT, progress_color="#A6B9DF",
                               button_hover_color=SUB, command=self.set_opacity)
        slider.set(self.opacity)
        slider.pack(fill="x", padx=6, pady=(4, 8))
        self.entries = {}
        for name, account in (("DeepSeek API Key", "deepseek_key"), ("腾讯云 AppID", "tencent_appid"),
                              ("腾讯云 SecretId", "tencent_id"), ("腾讯云 SecretKey", "tencent_key")):
            caption(name)
            entry = ctk.CTkEntry(form, show="*", border_width=1, border_color=BORDER, fg_color="white", text_color=INK)
            entry.pack(fill="x", padx=2, pady=(0, 3))
            try:
                entry.insert(0, keyring.get_password(SERVICE, account) or "")
            except Exception:
                entry.configure(state="disabled")
            self.entries[account] = entry

    def set_opacity(self, value):
        self.opacity = float(value)
        self.attributes("-alpha", self.opacity)

    def save_settings(self):
        try:
            for account, entry in self.entries.items():
                if entry.cget("state") == "disabled":
                    continue
                value = entry.get().strip()
                if value:
                    keyring.set_password(SERVICE, account, value)
                else:
                    try:
                        keyring.delete_password(SERVICE, account)
                    except keyring.errors.PasswordDeleteError:
                        pass
        except Exception as exc:
            self.status.set(f"无法保存凭据 · {type(exc).__name__}")
            return
        self.set_panel(self.before_settings)
        self.status.set("设置已保存")

    def _select(self, sid):
        self.view_id = sid
        self._header_state()
        self.transcript.configure(state="normal")
        self.transcript.delete("1.0", "end")
        self.transcript.mark_set("draft", "1.0")
        self.transcript.mark_gravity("draft", "left")
        self.transcript.configure(state="disabled")
        segments = self.history_store.segments(sid) if sid else []
        self.has_text = False
        for row in segments:
            self._write_segment(*row, final=True, scroll=False)
        if self.recording and sid == self.session_id and any(self.partial):
            self._write_segment(time.time(), None, *self.partial, final=False)
        if not self.has_text:
            self.scroll.pack_forget()
            self.transcript.pack_forget()
            self.empty_title.configure(text="正在聆听" if self.recording and sid == self.session_id else "开始一段同传" if sid is None else "暂无字幕")
            self.empty_hint.configure(text="识别到的原文和译文会逐段显示" if self.recording and sid == self.session_id else
                                      "原文与译文将在这里逐段显示" if sid is None else "这段会话还没有已保存的内容")
            self.empty.place(relx=.5, rely=.48, anchor="center")
        self.transcript.yview_moveto(1. if sid == self.session_id else 0.)
        self._history_list()
        self._dock_state()

    def _header_state(self):
        info = self.history_store.session(self.view_id) if self.view_id else None
        self.heading.configure(text="中英同传" if not info else f"{datetime.fromtimestamp(info[1]):%m月%d日}同传")
        if info:
            start = datetime.fromtimestamp(info[1])
            end = datetime.fromtimestamp(info[2]) if info[2] else None
            finish = (end.strftime("%H:%M:%S") if end.date() == start.date() else clock_text(info[2])) if end else "进行中"
            self.meta.configure(text=f"{start:%Y/%m/%d  %H:%M:%S}  –  {finish}")
        else:
            self.meta.configure(text="新会话")
        self.export_btn.configure(state="normal" if info else "disabled")

    def _write_segment(self, started, ended, original, translated, *, final, scroll=True):
        box = self.transcript
        at_bottom = box.yview()[1] >= .975
        if not self.has_text:
            self.empty.place_forget()
            self.scroll.pack(side="right", fill="y")
            box.pack(fill="both", expand=True)
            self.has_text = True
        box.configure(state="normal")
        box.delete("draft", "end-1c")
        stamp = datetime.fromtimestamp(started).strftime("%H:%M:%S")
        if ended:
            stamp += datetime.fromtimestamp(ended).strftime("  –  %H:%M:%S")
        box.insert("end-1c", stamp + "\n", "stamp")
        box.insert("end-1c", original + "\n", "original")
        box.insert("end-1c", (translated or "翻译中…") + "\n", "translation")
        if final:
            box.mark_set("draft", "end-1c")
        box.configure(state="disabled")
        if scroll and at_bottom:
            box.see("end")

    def select_session(self, sid):
        self.status.set("")
        self._select(sid)

    def new_session(self):
        if self.recording:
            self.status.set("请先点击头像暂停当前录制")
            return
        self.session_id = None
        if self.panel == "export":
            self.set_panel(None)
        self.select_session(None)

    def export(self):
        sid = self.view_id
        if not sid:
            return
        folder = Path(self.export_folder.get().strip()).expanduser()
        if not folder.is_absolute():
            self.status.set("请输入完整的保存文件夹路径")
            return
        _, start, end, _ = self.history_store.session(sid)
        stem = f"会话_{datetime.fromtimestamp(start):%Y%m%d_%H%M%S}_{sid}"
        rows = self.history_store.segments(sid)
        try:
            folder.mkdir(parents=True, exist_ok=True)
            for suffix, index in (("原文", 2), ("译文", 3)):
                lines = [f"开始：{clock_text(start)}", f"结束：{clock_text(end) if end else '进行中'}", ""]
                lines += [f"[{clock_text(r[0])} – {clock_text(r[1])}] {r[index]}" for r in rows]
                (folder / f"{stem}_{suffix}.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
        except OSError as exc:
            self.status.set(f"导出失败 · {exc.strerror}")
            return
        self.status.set("已分别导出原文和译文")

    def toggle(self):
        if self.closing:
            return
        if self.recording:
            self.stop_event.set()
            self._close_stream()
            self._dock_state()
            self.status.set("正在保存最后一段…")
            return
        deepseek = self.mode.get().startswith("DeepSeek")
        credentials = ["deepseek_key"] if deepseek else ["tencent_appid", "tencent_id", "tencent_key"]
        try:
            missing = any(not keyring.get_password(SERVICE, name) for name in credentials)
        except Exception:
            self.status.set("无法读取 Windows 凭据，请检查设置")
            return
        if missing or self.device.get() not in self.devices:
            self.status.set("请在左侧填写当前翻译服务的密钥" if missing else "请在左侧选择可用的麦克风")
            if self.panel != "settings":
                self.toggle_settings()
            return
        self.audio = queue.Queue(maxsize=100)
        self.stop_event.clear()
        try:
            self.stream = sd.RawInputStream(samplerate=SAMPLE_RATE, channels=1, dtype="int16",
                                           blocksize=FRAME_SAMPLES, device=self.devices[self.device.get()], callback=self.capture)
            self.stream.start()
            if self.view_id is None:
                sid = self.history_store.begin(self.mode.get())
            else:
                sid = self.view_id
                self.history_store.resume(sid)
        except Exception as exc:
            self._close_stream()
            self.status.set(f"无法开始录制 · {exc}")
            return
        self.session_id = sid
        self.recording = True
        self.partial = ("", "")
        self.capture_started = time.time()
        self.status.set("正在收音")
        self._select(sid)
        if self.panel == "settings":
            for menu in self.options:
                menu.configure(state="disabled")
        backend = AudioBackend.deepseek_loop if deepseek else AudioBackend.tencent_loop
        self.worker = threading.Thread(target=self._run, args=(backend,), daemon=True)
        self.worker.start()

    def _run(self, backend):
        try:
            backend(self)
        except Exception as exc:
            self.events.put(("status", f"录制中断 · {type(exc).__name__}"))
        finally:
            self.events.put(("finished",))

    def _close_stream(self):
        if self.stream is not None:
            stream, self.stream = self.stream, None
            try:
                stream.stop()
            finally:
                stream.close()

    def poll(self):
        while not self.events.empty():
            e = self.events.get_nowait()
            if e[0] in ("source", "pair", "final_pair"):
                if e[0] == "source":
                    start, end, src, dst, sid = time.time(), None, e[1], "", self.session_id
                else:
                    _, start, end, src, dst, sid = e
                final = e[0] == "final_pair"
                if sid is None or not (src or dst):
                    continue
                self.partial = ("", "") if final else (src, dst)
                if final:
                    self.history_store.append(sid, start, end, src, dst)
                if self.view_id == sid:
                    self._write_segment(start, end, src, dst, final=final)
                if final and self.panel == "history" and sid not in self.titled_sessions:
                    self.titled_sessions.add(sid)
                    self._history_list()
            elif e[0] == "status":
                self.status.set(e[1])
            elif e[0] == "finished":
                self._close_stream()
                self.history_store.end(self.session_id)
                self.recording = False
                had_partial = any(self.partial)
                self.partial = ("", "")
                if self.view_id == self.session_id and (had_partial or not self.has_text):
                    self._select(self.view_id)
                else:
                    self._header_state()
                    self._dock_state()
                if self.status.get() in ("正在收音", "正在保存最后一段…"):
                    self.status.set("已暂停")
                if self.panel == "settings":
                    for menu in self.options:
                        menu.configure(state="normal")
        if self.closing and not self.recording:
            self.destroy()
        else:
            self.after(80, self.poll)

    def close(self):
        self.closing = True
        if self.recording:
            self.stop_event.set()
            self._close_stream()
            self._dock_state()
            self.status.set("正在保存最后一段…")
        else:
            self.destroy()


def main():
    if sys.platform != "win32":
        InterpreterWindow().mainloop()
        return

    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
    kernel.CreateMutexW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.CreateMutexW(None, False, r"Local\ZhEnInterpreter.ConversationWindow")
    already_open = ctypes.get_last_error() == 183
    try:
        if handle and already_open:
            windows = ctypes.windll.user32
            windows.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
            windows.FindWindowW.restype = wintypes.HWND
            for _ in range(40):
                hwnd = windows.FindWindowW("TkTopLevel", "中英同传字幕")
                if hwnd:
                    if windows.IsIconic(wintypes.HWND(hwnd)):
                        windows.ShowWindow(wintypes.HWND(hwnd), 9)
                    windows.SetForegroundWindow(wintypes.HWND(hwnd))
                    break
                time.sleep(.1)
        else:
            InterpreterWindow().mainloop()
    finally:
        if handle:
            kernel.CloseHandle(handle)


if __name__ == "__main__":
    main()
