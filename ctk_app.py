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
from routes import KEYS, priority_loop, priority_order

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


def default_orb_position(main, size, scale):
    """Bottom-right of the main window's monitor work area, in Tk screen pixels."""
    import ctypes
    from ctypes import wintypes

    class MonitorInfo(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("monitor", wintypes.RECT),
                    ("work", wintypes.RECT), ("flags", wintypes.DWORD)]

    user32 = ctypes.windll.user32
    user32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
    user32.MonitorFromWindow.restype = wintypes.HANDLE
    user32.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.POINTER(MonitorInfo)]
    monitor = user32.MonitorFromWindow(main.winfo_id(), 2)
    info = MonitorInfo()
    info.cbSize = ctypes.sizeof(info)
    area = info.work if user32.GetMonitorInfoW(monitor, ctypes.byref(info)) else wintypes.RECT(
        0, 0, main.winfo_screenwidth(), main.winfo_screenheight())
    margin = round(12 * scale)
    return max(area.left, area.right - size - margin), max(area.top, area.bottom - size - margin)


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


def sidebar_icon():
    im = Image.new("RGBA", (88, 88))
    pen = ImageDraw.Draw(im)
    pen.rounded_rectangle((8, 14, 80, 74), radius=15, outline="#898E97", width=6)
    pen.line((32, 17, 32, 71), fill="#898E97", width=5)
    return ctk.CTkImage(im, size=(22, 22))


def history_action_icons():
    stroke = "#858990"
    edit = Image.new("RGBA", (88, 88))
    pen = ImageDraw.Draw(edit)
    pen.rounded_rectangle((9, 17, 70, 77), radius=13, outline=stroke, width=5)
    pen.rectangle((36, 7, 78, 46), fill=(0, 0, 0, 0))
    pen.line([(30, 55), (33, 43), (61, 15), (66, 13), (71, 17),
              (72, 22), (43, 52), (30, 55)], fill=stroke, width=5, joint="curve")
    trash = Image.new("RGBA", (88, 88))
    pen = ImageDraw.Draw(trash)
    pen.rounded_rectangle((33, 13, 55, 25), radius=5, outline=stroke, width=5)
    pen.line((14, 25, 74, 25), fill=stroke, width=5)
    pen.line([(22, 26), (26, 70), (30, 75), (58, 75), (62, 70), (66, 26)],
             fill=stroke, width=5, joint="curve")
    for x in (37, 51):
        pen.line((x, 36, x, 62), fill=stroke, width=4)
    return [ctk.CTkImage(im, size=(19, 19)) for im in (edit, trash)]


class RecordOrb(tk.Canvas):
    """Only an unmoved short press can toggle. A drag stays a drag until release."""

    def __init__(self, parent, orbs, scale, background, command, on_drag=None):
        size = round(60 * scale)
        super().__init__(parent, width=size, height=size, bg=background, borderwidth=0,
                         highlightthickness=0, takefocus=on_drag is None, cursor="hand2")
        self.images = [orb.create_scaled_photo_image(scale, "light") for orb in orbs]
        self.face = self.create_image(0, 0, anchor="nw", image=self.images[0])
        self.command = command
        self.on_drag = on_drag
        self.press_at = None
        self.enabled = True
        self.bind("<ButtonPress-1>", self._press)
        self.bind("<B1-Motion>", self._motion)
        self.bind("<ButtonRelease-1>", self._release)
        self.bind("<Return>", lambda _: self.invoke())
        self.bind("<space>", lambda _: self.invoke())

    def set_state(self, recording, enabled):
        self.enabled = enabled
        self.itemconfigure(self.face, image=self.images[int(recording)])

    def invoke(self):
        if self.enabled:
            self.command()

    def _press(self, event):
        self.press_at = (event.x_root, event.y_root)
        self.down_time = time.monotonic()
        self.press_timestamp = event.time
        self.dragged = False
        top = self.winfo_toplevel()
        self.window_origin = (top.winfo_x(), top.winfo_y())
        self.grab_set()

    def _motion(self, event):
        if self.press_at is None:
            return
        dx = event.x_root - self.press_at[0]
        dy = event.y_root - self.press_at[1]
        if abs(dx) + abs(dy) >= 2:
            self.dragged = True
        if self.dragged and self.on_drag:
            x, y = self.window_origin
            self.on_drag(x + dx, y + dy)

    def _release(self, event):
        if self.press_at is None:
            return
        self._motion(event)
        self.press_at = None
        if self.grab_current() is self:
            self.grab_release()
        elapsed_ms = (event.time - self.press_timestamp) & 0xffffffff
        if not self.dragged and elapsed_ms < 400 and time.monotonic() - self.down_time < .40:
            self.invoke()


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
        self.attributes("-topmost", False)
        self.attributes("-alpha", self.opacity)
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.history_store = history_store or HistoryStore()
        self.priority = priority_order(self.history_store.preference("priority", "DeepSeek|Kimi 订阅|腾讯云"))
        self.float_enabled = ctk.BooleanVar(value=self.history_store.preference("overlay", "1") == "1")
        self.overlay = None
        self.ball_position = None
        self.draft_title = ""
        self.session_id = self.view_id = None
        self.recording = self.closing = False
        self.panel = None
        self.before_settings = None
        self.export_folder = ctk.StringVar(value=str(Path.home() / "Documents" / "同传记录"))
        self.zoom = 1.
        self.worker = self.stream = None
        self.capture_started = 0.
        self.capture_ended = None
        self.stop_event = threading.Event()
        self.events = queue.Queue()
        self.audio = queue.Queue(maxsize=100)
        self.partial = ("", "")
        self.titled_sessions = set()
        self.devices = {f"{i} · {d['name']}": i for i, d in enumerate(sd.query_devices()) if d["max_input_channels"] > 0}
        self.device = ctk.StringVar(value=next(iter(self.devices), "无可用麦克风"))
        saved_mode = self.history_store.preference("translation_cadence", "")
        self.mode = ctk.StringVar(value="腾讯云 实时" if saved_mode == "腾讯云 实时" else "停顿翻译")
        self.status = ctk.StringVar(value="")
        portrait = Image.open(ASSETS / "icon.png").convert("RGBA")
        self.orbs = [orb_image(portrait, active) for active in (False, True)]
        self.wave = wave_image()
        self.edit_icon, self.delete_icon = history_action_icons()
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
        self.after_idle(self.update_overlay)
        self.after(80, self.poll)

    def _build_header(self):
        bar = ctk.CTkFrame(self.main, fg_color="transparent")
        bar.grid(row=0, column=0, sticky="ew", padx=14, pady=(12, 0))
        self.history_toggle = button(bar, "", self.toggle_history, 34)
        self.history_toggle.configure(image=sidebar_icon(), corner_radius=7)
        self.history_toggle.pack(side="left", padx=(0, 10))
        title_box = ctk.CTkFrame(bar, fg_color="transparent")
        title_box.pack(side="left", fill="x", expand=True)
        self.heading = label(title_box, "", 16, bold=True, anchor="w")
        self.heading.pack(fill="x")
        self.heading.bind("<Double-Button-1>", lambda _: self.edit_title())
        self.title_entry = ctk.CTkEntry(title_box, height=26, fg_color="white", border_color=BORDER, text_color=INK)
        self.title_entry.bind("<Return>", lambda _: self.finish_title())
        self.title_entry.bind("<Escape>", lambda _: self.cancel_title())
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
            ("stamp", 9, MUTED, 10, 3, "normal"), ("original", 11, SUB, 0, 4, "normal"),
            ("translation", 11, INK, 0, 12, "normal")):
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
        self.orb = RecordOrb(dock, self.orbs, self.main._get_widget_scaling(), PAPER, self.toggle)
        self.orb.grid(row=1, column=1, padx=3, pady=3)

    def _dock_state(self):
        stopping = self.recording and self.stop_event.is_set()
        text = "正在保存…" if stopping else "点击头像暂停" if self.recording else "点击头像继续" if self.view_id else "点击头像录制"
        self.action.configure(text=text)
        self.orb.set_state(self.recording, not stopping)
        if self.overlay is not None:
            self.float_canvas.set_state(self.recording, not stopping)
        if self.recording and self.view_id != self.session_id:
            self.action.pack_forget()
            self.live_link.pack(side="left")
        else:
            self.live_link.pack_forget()
            if not self.action.winfo_manager():
                self.action.pack(side="left", fill="x", expand=True)

    def toggle_history(self):
        self.set_panel(None if self.panel == "history" else "history")

    def close_overlay(self):
        if self.overlay is not None:
            self.overlay.destroy()
            self.overlay = None

    def update_overlay(self):
        if not self.float_enabled.get():
            self.close_overlay()
            return
        if self.overlay is not None:
            return
        # Use a colour-keyed window: the only visible and clickable surface is the portrait.
        chroma = "#FF00FF"
        win = self.overlay = tk.Toplevel(self, background=chroma, takefocus=False)
        win.withdraw()
        win.title("录制悬浮球")
        win.overrideredirect(True)
        win.attributes("-toolwindow", True)
        win.attributes("-transparentcolor", chroma)
        win.attributes("-topmost", True)
        scale = self.main._get_widget_scaling()
        size = round(60 * scale)
        def move_to(x, y):
            self.ball_position = (x, y)
            win.geometry(f"+{x}+{y}")
        def click():
            if self.closing:
                return
            self.toggle()
            if not self.recording:
                self.deiconify()
                self.lift()
        self.float_canvas = RecordOrb(win, self.orbs, scale, chroma, click, move_to)
        self.float_canvas.set_state(self.recording, not self.stop_event.is_set() or not self.recording)
        self.float_canvas.pack()
        x, y = self.ball_position or default_orb_position(self, size, scale)
        win.geometry(f"{size}x{size}+{x}+{y}")
        win.deiconify()

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
        self.history_toggle.configure(fg_color=SELECT if name == "history" else "transparent")
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
        for sid, started, title in rows:
            date = datetime.fromtimestamp(started)
            day = "今天" if date.date() == datetime.now().date() else date.strftime("%Y/%m/%d")
            if day != group:
                label(self.listing, day, 11, MUTED).pack(anchor="w", padx=8, pady=(12, 4))
                group = day
            card = ctk.CTkFrame(self.listing, fg_color=SELECT if sid == self.view_id else "transparent", corner_radius=10)
            card.pack(fill="x", pady=2)
            text = title or date.strftime("%m.%d %H:%M")
            for icon, command in (
                (self.delete_icon, lambda s=sid, c=card: self.confirm_delete(s, c)),
                (self.edit_icon, lambda s=sid: self.rename_from_history(s)),
            ):
                action = button(card, "", command, 26)
                action.configure(image=icon, corner_radius=5, border_spacing=0)
                action.pack(side="right", padx=1)
            button(card, text, lambda s=sid: self.select_session(s), width=36, anchor="w").pack(side="left", fill="x", expand=True)

    def rename_from_history(self, sid):
        self.select_session(sid)
        self.edit_title()

    def edit_title(self):
        self.title_entry.delete(0, "end")
        self.title_entry.insert(0, self.history_store.title(self.view_id) if self.view_id else self.draft_title)
        self.heading.pack_forget()
        self.title_entry.pack(fill="x", before=self.meta)
        self.title_entry.focus_set()
        self.title_entry.select_range(0, "end")
        self.status.set("输入名称，按回车保存；清空可恢复开始时间")

    def cancel_title(self):
        self.title_entry.pack_forget()
        self.heading.pack(fill="x", before=self.meta)
        return "break"

    def finish_title(self):
        value = self.title_entry.get().strip()
        if self.view_id is None:
            self.draft_title = value
        else:
            self.history_store.rename(self.view_id, value)
        self.cancel_title()
        self._header_state()
        self._history_list()
        self.status.set("名称已保存")

    def confirm_delete(self, sid, actions):
        if self.recording and self.session_id == sid:
            self.status.set("请先暂停录制，再删除这段会话")
            return
        for child in actions.winfo_children():
            child.destroy()
        button(actions, "确认删除", lambda: self.delete_session(sid), 68, fill="#F3DFE6").pack(side="left")
        button(actions, "取消", self._history_list, 42).pack(side="left")

    def delete_session(self, sid):
        if self.recording and self.session_id == sid:
            return
        self.history_store.delete_session(sid)
        if self.session_id == sid:
            self.session_id = None
        if self.view_id == sid:
            self.draft_title = ""
            self._select(self.session_id)
        else:
            self._history_list()
        self.status.set("会话已删除")

    def _settings(self):
        save = button(self.side, "保存设置", self.save_settings, fill=SELECT)
        save.pack(side="bottom", fill="x", padx=14, pady=10)
        form = ctk.CTkScrollableFrame(self.side, fg_color="transparent", scrollbar_button_color=BORDER)
        form.pack(fill="both", expand=True, padx=(8, 2))
        def caption(text):
            label(form, text, 11, SUB).pack(anchor="w", padx=4, pady=(12, 4))
        self.options = []
        for name, var, values in (("录制模式", self.mode, ["停顿翻译", "腾讯云 实时"]),
                                   ("麦克风", self.device, list(self.devices) or ["无可用麦克风"])):
            caption(name)
            menu = ctk.CTkOptionMenu(form, variable=var, values=values, width=110, dynamic_resizing=False,
                                     fg_color="white", button_color="#DCE5F6", button_hover_color=SELECT,
                                     text_color=INK, dropdown_fg_color=PAPER, dropdown_text_color=INK,
                                     state="disabled" if self.recording else "normal", font=font(11))
            menu.pack(fill="x", padx=2)
            self.options.append(menu)
        caption("API 优先级 · 从上到下")
        order_box = ctk.CTkFrame(form, fg_color="transparent")
        order_box.pack(fill="x")
        self.pending_priority = list(self.priority)
        def draw_order():
            for child in order_box.winfo_children():
                child.destroy()
            for index, name in enumerate(self.pending_priority):
                row = ctk.CTkFrame(order_box, fg_color="transparent")
                row.pack(fill="x")
                label(row, f"{index + 1}  {name}", 11).pack(side="left", padx=3)
                if index:
                    button(row, "上移", lambda i=index: move(i), 40).pack(side="right")
        def move(index):
            order = self.pending_priority
            order[index - 1], order[index] = order[index], order[index - 1]
            draw_order()
        draw_order()
        label(form, "跳过未填密钥的服务。当前服务失败时，本段尝试下一项。", 11, MUTED,
              wraplength=147, justify="left").pack(anchor="w", pady=5)
        ctk.CTkCheckBox(form, text="显示桌面悬浮球", variable=self.float_enabled, command=self.update_overlay,
                        text_color=SUB, font=font(12)).pack(anchor="w", pady=(8, 2))
        caption("窗口透明度")
        slider = ctk.CTkSlider(form, from_=.55, to=1., width=100, button_color=ACCENT, progress_color="#A6B9DF",
                               button_hover_color=SUB, command=self.set_opacity)
        slider.set(self.opacity)
        slider.pack(fill="x", padx=6, pady=(4, 8))
        self.entries = {}
        for name, account in (("Kimi Code 订阅 Key", "kimi_key"), ("DeepSeek API Key", "deepseek_key"), ("腾讯云 AppID", "tencent_appid"),
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
        self.priority = list(self.pending_priority)
        self.history_store.set_preference("priority", "|".join(self.priority))
        self.history_store.set_preference("translation_cadence", self.mode.get())
        self.history_store.set_preference("overlay", str(int(self.float_enabled.get())))
        self.set_panel(self.before_settings)
        self.status.set("设置已保存")

    def _select(self, sid):
        self.cancel_title()
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
        self.heading.configure(text=self.history_store.title(self.view_id) if info else self.draft_title or "中英同传")
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
        box.insert("end-1c", (translated or ("本段未取得译文" if final else "翻译中…")) + "\n", "translation")
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
        self.draft_title = ""
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
                lines = [self.history_store.title(sid), f"开始：{clock_text(start)}", f"结束：{clock_text(end) if end else '进行中'}", ""]
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
            self._stop_capture()
            self._dock_state()
            self.status.set("正在保存最后一段…")
            return
        realtime = self.mode.get() == "腾讯云 实时"
        candidates = ["腾讯云"] if realtime else self.priority
        try:
            self.route_keys = {key: keyring.get_password(SERVICE, key) for name in candidates for key in KEYS[name]}
            self.active_routes = [name for name in candidates if all(self.route_keys[key] for key in KEYS[name])]
            missing = not self.active_routes
        except Exception:
            self.status.set("无法读取 Windows 凭据，请检查设置")
            return
        if missing or self.device.get() not in self.devices:
            self.status.set("请在左侧填写当前翻译服务的密钥" if missing else "请在左侧选择可用的麦克风")
            if self.panel != "settings":
                self.toggle_settings()
            return
        self.audio = queue.Queue(maxsize=100) if realtime else queue.SimpleQueue()
        self.stop_event.clear()
        self.capture_ended = None
        try:
            self.stream = sd.RawInputStream(samplerate=SAMPLE_RATE, channels=1, dtype="int16",
                                           blocksize=FRAME_SAMPLES, device=self.devices[self.device.get()], callback=self.capture)
            self.capture_started = time.time()
            self.stream.start()
            if self.view_id is None:
                sid = self.history_store.begin(self.active_routes[0], self.draft_title or None)
                self.draft_title = ""
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
        self.status.set("正在收音" if realtime else "正在收音 · 停顿 1 秒后翻译")
        self._select(sid)
        self.update_overlay()
        if self.panel == "settings":
            for menu in self.options:
                menu.configure(state="disabled")
        backend = AudioBackend.tencent_loop if realtime else priority_loop
        self.worker = threading.Thread(target=self._run, args=(backend,), daemon=True)
        self.worker.start()

    def _run(self, backend):
        try:
            backend(self)
        except Exception as exc:
            self.events.put(("status", f"录制中断 · {type(exc).__name__}"))
        finally:
            self.events.put(("finished",))

    def _stop_capture(self):
        if self.capture_ended is None:
            self.capture_ended = time.time()
        try:
            self._close_stream()
        finally:
            self.stop_event.set()

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
                self.history_store.end(self.session_id, self.capture_ended)
                self.recording = False
                self.route_keys.clear()
                had_partial = any(self.partial)
                self.partial = ("", "")
                if self.view_id == self.session_id and (had_partial or not self.has_text):
                    self._select(self.view_id)
                else:
                    self._header_state()
                    self._dock_state()
                if self.status.get().startswith("正在收音") or self.status.get() == "正在保存最后一段…":
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
            self._stop_capture()
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
