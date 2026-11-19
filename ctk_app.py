"""Single-window conversation workspace for the Windows interpreter."""
from __future__ import annotations

import queue
import re
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog
from datetime import datetime
from pathlib import Path

import customtkinter as ctk
import keyring
import sounddevice as sd
from PIL import Image, ImageDraw, ImageFont, ImageOps

from interpreter import FRAME_SAMPLES, SAMPLE_RATE, SERVICE, HistoryStore, Interpreter as AudioBackend, clock_text, safe_filename
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


def desktop_folder() -> Path:
    if sys.platform == "win32":
        import ctypes

        # Windows resolves the user's Desktop, including a redirected folder.
        desktop_directory = 0x10  # CSIDL_DESKTOPDIRECTORY
        buffer = ctypes.create_unicode_buffer(260)
        result = ctypes.windll.shell32.SHGetFolderPathW(None, desktop_directory, None, 0, buffer)
        if result == 0 and buffer.value:
            return Path(buffer.value)
    return Path.home() / "Desktop"


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
                         fg_color=fill, hover_color=SELECT, text_color=SUB, corner_radius=8,
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


def sidebar_glyph(glyph):
    image = Image.new("RGBA", (64, 64))
    pen = ImageDraw.Draw(image)
    try:
        face = ImageFont.truetype("segmdl2.ttf", 48)
        bounds = face.getbbox(glyph)
        pen.text(((64 - bounds[2] - bounds[0]) / 2, (64 - bounds[3] - bounds[1]) / 2),
                 glyph, font=face, fill=SUB)
    except OSError:
        pen.rounded_rectangle((12, 12, 52, 52), radius=10, outline=SUB, width=4)
    return ctk.CTkImage(image, size=(16, 16))


def project_icon(opened=False):
    image = Image.new("RGBA", (64, 64))
    pen = ImageDraw.Draw(image)
    pen.line([(8, 49), (8, 19), (12, 15), (25, 15), (31, 21), (52, 21),
              (56, 25), (56, 49), (52, 53), (12, 53), (8, 49)],
             fill=SUB, width=4, joint="curve")
    if opened:
        pen.polygon([(9, 51), (17, 33), (60, 33), (54, 51), (50, 54), (12, 54)], fill=SIDE)
        pen.line([(9, 51), (17, 33), (60, 33), (54, 51), (50, 54), (12, 54), (9, 51)],
                 fill=SUB, width=4, joint="curve")
    return ctk.CTkImage(image, size=(17, 17))


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


class RecordingRun:
    """Capture and worker state for one uninterrupted microphone interval."""
    capture = AudioBackend.capture

    def __init__(self, history_store, keys, routes):
        self.history_store = history_store
        self.route_keys = keys
        self.active_routes = routes
        self.audio = queue.SimpleQueue()
        self.events = queue.SimpleQueue()
        self.stop_event = threading.Event()
        self.saved = threading.Event()
        self.previous_saved = None
        self.session_id = None
        self.capture_started = 0.
        self.failure = ""

    def process(self, backend):
        try:
            if backend is AudioBackend.tencent_loop and self.previous_saved is not None:
                self.previous_saved.wait()
            backend(self)
        except Exception as exc:
            self.events.put(("status", f"录制中断 · {type(exc).__name__}"))
        finally:
            self.events.put(("finished",))


class InterpreterWindow(ctk.CTk):

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
        self.expanded_projects = set()
        self.archive_open = False
        self.recent_open = True
        self.history_action = None
        self.project_form = None
        self.project_name = ctk.StringVar(value="")
        self.draft_project = None
        self.settings_page = self.export_page = None
        self.export_results = queue.Queue()
        self.exporting_project = False
        self.desktop_folder = desktop_folder()
        self.export_folder = ctk.StringVar(value=str(self.desktop_folder))
        self.zoom = 1.
        self.stream = None
        self.current_run = None
        self.pending_runs = []
        self.partial = ("", "")
        self.devices = {f"{i} · {d['name']}": i for i, d in enumerate(sd.query_devices()) if d["max_input_channels"] > 0}
        self.device = ctk.StringVar(value=next(iter(self.devices), "无可用麦克风"))
        saved_mode = self.history_store.preference("translation_cadence", "")
        self.mode = ctk.StringVar(value="腾讯云 实时" if saved_mode == "腾讯云 实时" else "停顿翻译")
        self.status = ctk.StringVar(value="")
        portrait = Image.open(ASSETS / "icon.png").convert("RGBA")
        self.orbs = [orb_image(portrait, active) for active in (False, True)]
        self.wave = wave_image()
        self.folder_icons = (project_icon(), project_icon(opened=True))
        self.sidebar_font = font(12)
        self.sidebar_icons = {glyph: sidebar_glyph(glyph) for glyph in ("\ue70f", "\ue713", "\ue7b8")}
        self.grid_rowconfigure(0, weight=1)
        self.grid_columnconfigure(1, weight=1)
        self.side = ctk.CTkFrame(self, width=224, corner_radius=0, fg_color=SIDE)
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
        self.set_panel("history")
        self.bind("<Escape>", self._escape)
        self.bind("<Control-b>", lambda _: self.toggle_history())
        self.after_idle(self.update_overlay)
        self.after(80, self.poll)

    def _build_header(self):
        bar = ctk.CTkFrame(self.main, fg_color="transparent")
        bar.grid(row=0, column=0, sticky="ew", padx=14, pady=(12, 0))
        self.history_toggle = button(bar, "", self.toggle_history, 34)
        self.history_toggle.configure(image=sidebar_icon(), corner_radius=7)
        self.history_toggle.pack(side="left", padx=(0, 10))
        title_box = self.title_box = ctk.CTkFrame(bar, fg_color="transparent")
        title_box.pack(side="left", fill="x", expand=True)
        self.heading = label(title_box, "", 16, bold=True, anchor="w")
        self.heading.pack(fill="x")
        self.heading.bind("<Double-Button-1>", lambda _: self.edit_title())
        self.title_entry = ctk.CTkEntry(title_box, height=26, fg_color="white", border_color=BORDER, text_color=INK)
        self.title_entry.bind("<Return>", lambda _: self.finish_title())
        self.title_entry.bind("<Escape>", lambda _: self.cancel_title())
        self.meta = label(title_box, "", 10, MUTED, anchor="w")
        self.meta.pack(fill="x")
        self.settings_btn = button(bar, "\ue713", self.toggle_settings, 32, symbol=True)
        self.settings_btn.pack(side="right")
        self.export_btn = button(bar, "导出", self.toggle_export, 44)
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

        box.tag_configure("english", font=(FONT, max(8, round(11 * factor * self.zoom)) + 1, "normal"))
        box.tag_raise("english")

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
        text = "点击头像暂停" if self.recording else "点击头像继续" if self.view_id else "点击头像录制"
        self.action.configure(text=text)
        self.orb.set_state(self.recording, not self.closing)
        if self.overlay is not None:
            self.float_canvas.set_state(self.recording, not self.closing)
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
        self.float_canvas.set_state(self.recording, not self.closing)
        self.float_canvas.pack()
        x, y = self.ball_position or default_orb_position(self, size, scale)
        win.geometry(f"{size}x{size}+{x}+{y}")
        win.deiconify()

    def toggle_settings(self):
        if self.settings_page is not None:
            self.close_settings()
            return
        self.close_export()
        if self.history_action or self.project_form:
            self._dismiss_history_action()
        self.settings_page = ctk.CTkFrame(self.main, corner_radius=0, fg_color=PAPER)
        self.settings_page.grid(row=0, column=0, rowspan=2, sticky="nsew")
        self.settings_page.lift()
        self._settings(self.settings_page)

    def close_settings(self):
        if self.settings_page is not None:
            self.settings_page.destroy()
            self.settings_page = self.settings_tab = None

    def _escape(self, _event):
        if self.project_form or self.history_action:
            self._dismiss_history_action()
        elif self.settings_page is not None:
            self.close_settings()
        elif self.export_page is not None:
            self.close_export()
        else:
            self.set_panel(None)
        return "break"

    def toggle_export(self):
        if self.export_page is not None:
            self.close_export()
        elif self.view_id is not None:
            self.close_settings()
            page = self.export_page = ctk.CTkFrame(self.main, corner_radius=0, fg_color=PAPER)
            page.grid(row=0, column=0, rowspan=2, sticky="nsew")
            page.lift()
            header = ctk.CTkFrame(page, fg_color="transparent")
            header.pack(fill="x", padx=18, pady=(9, 4))
            self._page_heading(header, "导出会话", self.close_export)
            label(page, "保存位置", 12, bold=True).pack(anchor="w", padx=24, pady=(8, 4))
            field = ctk.CTkFrame(page, fg_color="transparent")
            field.pack(fill="x", padx=24)
            button(field, "浏览…", self.choose_export_folder, 58).pack(side="right", padx=(5, 0))
            entry = ctk.CTkEntry(field, textvariable=self.export_folder, border_width=1,
                                 border_color=BORDER, fg_color="white", text_color=INK)
            entry.pack(side="left", fill="x", expand=True)
            label(page, "原文和译文各保存一个 TXT，保留每段时间。", 11, MUTED).pack(anchor="w", padx=24, pady=8)
            button(page, "保存 TXT", self.export, 76, fill=SELECT).pack(anchor="w", padx=24)

    def close_export(self):
        if self.export_page is not None:
            self.export_page.destroy()
            self.export_page = None

    def choose_export_folder(self):
        folder = Path(self.export_folder.get().strip()).expanduser()
        path = filedialog.askdirectory(parent=self, title="选择保存位置",
                                       initialdir=str(folder if folder.is_absolute() and folder.is_dir() else self.desktop_folder))
        if path:
            self.export_folder.set(path)

    def _page_heading(self, parent, title, back):
        button(parent, "\ue72b", back, 28, symbol=True).pack(side="left")
        label(parent, title, 17, bold=True).pack(side="left", padx=7)

    def set_panel(self, name):
        self.panel = name
        self.history_action = self.project_form = None
        for child in self.side.winfo_children():
            child.destroy()
        if name is None:
            self.history_toggle.pack(side="left", before=self.title_box, padx=(0, 10))
            self.side.grid_remove()
            return
        self.history_toggle.pack_forget()
        self.side.grid(row=0, column=0, sticky="ns")
        row = ctk.CTkFrame(self.side, fg_color="transparent")
        row.pack(fill="x", padx=(15, 9), pady=(10, 4))
        label(row, "中英同传", 14, bold=True).pack(side="left")
        close = button(row, "", lambda: self.set_panel(None), 28)
        close.configure(image=sidebar_icon())
        close.pack(side="right")
        self._sidebar_button(self.side, "新会话", self.new_session, icon="\ue70f").pack(fill="x", padx=10)
        footer = ctk.CTkFrame(self.side, fg_color="transparent")
        footer.pack(side="bottom", fill="x", padx=10, pady=(1, 6))
        self._sidebar_button(footer, "设置", self.toggle_settings, icon="\ue713").pack(side="left")
        self._sidebar_button(footer, "已归档", self._toggle_archive, icon="\ue7b8").pack(side="right")
        self.listing = ctk.CTkScrollableFrame(self.side, fg_color="transparent", scrollbar_button_color=BORDER)
        self.listing.pack(fill="both", expand=True, padx=(9, 3), pady=(3, 0))
        self._history_list()

    def _sidebar_button(self, parent, title, command, icon=None, selected=False):
        control = button(parent, title, command, 28, anchor="w", fill=SELECT if selected else "transparent")
        control.configure(text_color=INK, height=29, border_spacing=5, font=self.sidebar_font)
        if isinstance(icon, str):
            control.configure(image=self.sidebar_icons[icon])
        elif icon is not None:
            control.configure(image=icon)
        return control

    def _shorten_title(self, text, width):
        text = " ".join(text.split())
        if self.sidebar_font.measure(text) <= width:
            return text
        left, right = 0, len(text)
        while left < right:
            middle = (left + right + 1) // 2
            if self.sidebar_font.measure(text[:middle] + "…") <= width:
                left = middle
            else:
                right = middle - 1
        return text[:left] + "…"

    def _history_list(self):
        if self.panel != "history":
            return
        canvas = self.listing._parent_canvas
        position = canvas.yview()[0]
        for child in self.listing.winfo_children():
            child.destroy()
        self.history_rows = {}
        projects = dict(self.history_store.projects())
        recent, archived, project_sessions = [], [], {}
        for sid, started, title, pid, is_archived in self.history_store.library_sessions():
            item = sid, title or datetime.fromtimestamp(started).strftime("%m.%d %H:%M"), pid
            if is_archived:
                archived.append(item)
            elif pid is None:
                recent.append(item)
            else:
                project_sessions.setdefault(pid, []).append(item)

        heading = ctk.CTkFrame(self.listing, fg_color="transparent")
        heading.pack(fill="x", pady=(3, 0))
        label(heading, "项目", 11, MUTED, bold=True).pack(side="left", padx=7)
        button(heading, "创建项目", lambda: self._open_project_form(None), 60).pack(side="right")
        if self.project_form == ("new", None):
            self._draw_project_form(self.listing, None)
        for pid, name in projects.items():
            opened = pid in self.expanded_projects
            key = ("project", pid)
            self._history_row(key, name, lambda n=pid: self._toggle_project(n), icon=self.folder_icons[opened],
                              selected=self.view_id is None and self.draft_project == pid)
            if self.history_action == (key, "menu"):
                self._history_menu([
                    ("在此新建会话", lambda n=pid: self.new_session(n)),
                    ("导出项目 ZIP", lambda n=pid: self._export_project(n)),
                ])
            if opened:
                for sid, title, _ in project_sessions.get(pid, ()):
                    self._session_row(sid, title, pid, projects, indent=18)
                if pid not in project_sessions:
                    label(self.listing, "暂无会话", 11, MUTED).pack(anchor="w", padx=24, pady=3)

        if self.archive_open:
            archive_header = self._sidebar_button(self.listing, "⌄  已归档", self._toggle_archive)
            archive_header.pack(fill="x", pady=(8, 0))
            self.history_rows["archive"] = archive_header
            for sid, title, pid in archived:
                self._session_row(sid, title, pid, projects, indent=12, archived=True)
            if not archived:
                label(self.listing, "暂无归档", 11, MUTED).pack(anchor="w", padx=16, pady=3)

        self._sidebar_button(self.listing, ("⌄" if self.recent_open else "›") + "  最近会话",
                             self._toggle_recent).pack(fill="x", pady=(8, 0))
        if self.recent_open:
            for sid, title, _ in recent:
                self._session_row(sid, title, None, projects)
            if not recent:
                label(self.listing, "暂无会话", 11, MUTED).pack(anchor="w", padx=10, pady=3)
        self.listing.update_idletasks()
        canvas.yview_moveto(position)

    def _history_row(self, key, title, command, *, icon=None, selected=False, indent=0):
        card = ctk.CTkFrame(self.listing, fg_color=SELECT if selected else "transparent", corner_radius=8)
        card.pack(fill="x", padx=(indent, 0), pady=1)
        self.history_rows[key] = card
        more = button(card, "⋯", lambda: self._show_history_action(key, "menu"), 25)
        more.configure(height=28, border_spacing=0, text_color=SUB if selected or
                       (self.history_action and self.history_action[0] == key) else SIDE)
        if not selected and not (self.history_action and self.history_action[0] == key):
            def hide(_event):
                x, y = card.winfo_pointerxy()
                left, top = card.winfo_rootx(), card.winfo_rooty()
                if not (left <= x < left + card.winfo_width() and top <= y < top + card.winfo_height()):
                    more.configure(text_color=SIDE)

            card.bind("<Enter>", lambda _: more.configure(text_color=SUB), add="+")
            card.bind("<Leave>", hide, add="+")
            more.bind("<Enter>", lambda _: more.configure(text_color=SUB), add="+")
            more.bind("<Leave>", hide, add="+")
        more.pack(side="right", padx=(0, 2))
        short = self._shorten_title(title, 135 - indent - (16 if icon else 0))
        toggle = self._sidebar_button(card, short, command, icon, selected)
        toggle.pack(side="left", fill="x", expand=True)
        if not selected and not (self.history_action and self.history_action[0] == key):
            toggle.bind("<Enter>", lambda _: more.configure(text_color=SUB), add="+")
            toggle.bind("<Leave>", hide, add="+")

    def _session_row(self, sid, title, project, projects, *, indent=0, archived=False):
        key = ("session", sid)
        self._history_row(key, title, lambda: self.select_session(sid), selected=sid == self.view_id, indent=indent)
        if self.project_form == ("new", sid):
            self._draw_project_form(self.listing, sid)
        if self.history_action == (key, "menu"):
            self._history_menu([
                ("重命名", lambda: self.rename_from_history(sid)),
                ("移入项目  ›", lambda: self._show_history_action(key, "projects")),
                ("导出 TXT", lambda: self._export_session(sid)),
                ("恢复会话" if archived else "归档", lambda: self._archive(sid, not archived)),
                ("删除", lambda: self.confirm_delete(sid)),
            ])
        elif self.history_action == (key, "projects"):
            actions = [(self._shorten_title(name, 130), lambda n=pid: self._move(sid, n))
                       for pid, name in projects.items() if pid != project]
            actions.append(("创建项目并移入", lambda: self._open_project_form(sid)))
            if project is not None:
                actions.append(("移出当前项目", lambda: self._move(sid, None)))
            actions.append(("‹  返回", lambda: self._show_history_action(key, "menu")))
            self._history_menu(actions)
        elif self.history_action == (key, "delete"):
            self._history_menu([("确认删除", lambda: self.delete_session(sid)), ("取消", self._dismiss_history_action)])

    def _history_menu(self, actions):
        menu = ctk.CTkFrame(self.listing, fg_color=PAPER, border_width=1, border_color=BORDER, corner_radius=8)
        menu.pack(fill="x", padx=(12, 4), pady=(0, 3))
        label_color = {"删除": "#A24460", "确认删除": "#A24460"}
        for text, command in actions:
            control = self._sidebar_button(menu, text, command)
            if text in label_color:
                control.configure(text_color=label_color[text])
            control.pack(fill="x", padx=3, pady=1)
        self._reveal_sidebar_widget(menu)

    def _reveal_sidebar_widget(self, widget):
        def reveal():
            if not widget.winfo_exists():
                return
            canvas = self.listing._parent_canvas
            region = canvas.bbox("all")
            if not region or not canvas.winfo_viewable():
                return
            height = canvas.winfo_height()
            top = widget.winfo_rooty() - canvas.winfo_rooty()
            if top < 0:
                shift = top
            else:
                shift = min(top, max(0, top + widget.winfo_height() - height))
            if shift:
                canvas.yview_moveto((canvas.canvasy(0) + shift) / max(1, region[3]))
        self.after_idle(reveal)

    def _show_history_action(self, key, action):
        self.project_form = None
        state = (key, action)
        self.history_action = None if self.history_action == state else state
        self._history_list()

    def _dismiss_history_action(self):
        self.history_action = self.project_form = None
        self._history_list()

    def _toggle_project(self, pid):
        self.expanded_projects.symmetric_difference_update({pid})
        self._dismiss_history_action()

    def _toggle_recent(self):
        self.recent_open = not self.recent_open
        self._dismiss_history_action()

    def _toggle_archive(self):
        self.archive_open = not self.archive_open
        self._dismiss_history_action()
        if self.archive_open:
            self._reveal_sidebar_widget(self.history_rows["archive"])

    def _archive(self, sid, archive):
        self.history_store.archive_session(sid, archive)
        self._dismiss_history_action()
        self.status.set("会话已归档" if archive else "会话已恢复")

    def _move(self, sid, pid):
        self.history_store.move_session(sid, pid)
        if pid is not None:
            self.expanded_projects.add(pid)
        else:
            self.recent_open = True
        self._dismiss_history_action()
        target = self.history_rows.get(("session", sid))
        if target is not None:
            self._reveal_sidebar_widget(target)
        self.status.set("已移入项目" if pid is not None else "已移出项目")

    def _open_project_form(self, sid):
        self.project_name.set("")
        self.history_action = None
        self.project_form = ("new", sid)
        self._history_list()
        self.project_entry.focus_set()

    def _draw_project_form(self, parent, sid):
        form = ctk.CTkFrame(parent, fg_color=PAPER, border_width=1, border_color=BORDER)
        form.pack(fill="x", padx=3, pady=4)
        entry = self.project_entry = ctk.CTkEntry(form, textvariable=self.project_name,
                                                 fg_color="white", border_color=BORDER)
        label(form, "项目名称", 11, MUTED).pack(anchor="w", padx=8, pady=(5, 0))
        entry.pack(fill="x", padx=6, pady=4)

        def save():
            try:
                pid = self.history_store.create_project(self.project_name.get())
            except ValueError as exc:
                feedback.configure(text=str(exc))
                return
            if sid is not None:
                self._move(sid, pid)
            else:
                self.expanded_projects.add(pid)
                self._dismiss_history_action()
                self._reveal_sidebar_widget(self.history_rows[("project", pid)])
                self.status.set("项目已创建")

        entry.bind("<Return>", lambda _: save())
        feedback = label(form, "", 10, "#A24460")
        feedback.pack(anchor="w", padx=6)
        controls = ctk.CTkFrame(form, fg_color="transparent")
        controls.pack(fill="x", padx=4, pady=(0, 4))
        button(controls, "取消", self._dismiss_history_action, 38).pack(side="right")
        button(controls, "创建并移入" if sid is not None else "创建", save, 60, fill=SELECT).pack(side="right", padx=3)
        self._reveal_sidebar_widget(form)

    def _export_project(self, pid):
        if self.exporting_project:
            self.status.set("正在导出项目，请稍候")
            return
        if not any(self.history_store.session_previews(pid, archived) for archived in (False, True)):
            self.status.set("项目里还没有会话")
            return
        name = dict(self.history_store.projects())[pid]
        folder = Path(self.export_folder.get().strip()).expanduser()
        path = filedialog.asksaveasfilename(parent=self, title="导出整个项目（含已归档会话）", defaultextension=".zip",
            filetypes=[("ZIP 压缩包", "*.zip")], initialdir=str(folder if folder.is_absolute() and folder.is_dir() else self.desktop_folder),
            initialfile=f"项目_{safe_filename(name)}_{datetime.now():%Y%m%d}.zip")
        if not path:
            return
        self.exporting_project = True
        self.status.set("正在打包已保存的原文和译文…")
        def write_zip():
            try:
                count = self.history_store.project_zip(pid, path)
                result = f"已导出 {count} 个会话：{Path(path).name}"
            except Exception as exc:
                result = f"导出失败：{exc}"
            self.export_results.put(result)
        threading.Thread(target=write_zip, name="project-export").start()

    def _export_session(self, sid):
        self.select_session(sid)
        self.toggle_export()

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

    def confirm_delete(self, sid):
        if any(run.session_id == sid for run in self.pending_runs):
            self.status.set("请等这段录制结束并保存后再删除")
            return
        self._show_history_action(("session", sid), "delete")

    def delete_session(self, sid):
        if any(run.session_id == sid for run in self.pending_runs):
            return
        self.history_store.delete_session(sid)
        self.history_action = None
        if self.session_id == sid:
            self.session_id = None
        if self.view_id == sid:
            self.draft_title = ""
            self.draft_project = None
            self.close_export()
            self._select(self.session_id)
        else:
            self._history_list()
        self.status.set("会话已删除")

    def _settings(self, page):
        header = ctk.CTkFrame(page, fg_color="transparent")
        header.pack(fill="x", padx=18, pady=(9, 4))
        self._page_heading(header, "设置", self.close_settings)
        button(header, "保存设置", self.save_settings, 72, fill=SELECT).pack(side="right")
        tabs = ctk.CTkFrame(page, fg_color="transparent")
        tabs.pack(fill="x", padx=18, pady=(0, 4))
        sections = {}
        for name in ("录制", "翻译服务", "外观"):
            frame = ctk.CTkScrollableFrame(page, fg_color="transparent", scrollbar_button_color=BORDER)
            tab = button(tabs, name, lambda n=name: show(n), 64)
            tab.pack(side="left", padx=(0, 3))
            sections[name] = (tab, frame)

        def show(name):
            for key, (tab, frame) in sections.items():
                tab.configure(fg_color=SELECT if key == name else "transparent")
                if key == name:
                    frame.pack(fill="both", expand=True, padx=(18, 12), pady=(0, 4))
                else:
                    frame.pack_forget()

        def caption(parent, text):
            label(parent, text, 12, INK, bold=True).pack(anchor="w", padx=3, pady=(5, 3))

        recording = sections["录制"][1]
        self.options = []
        for name, var, values in (
            ("麦克风", self.device, list(self.devices) or ["无可用麦克风"]),
            ("录制模式", self.mode, ["停顿翻译", "腾讯云 实时"]),
        ):
            caption(recording, name)
            menu = ctk.CTkOptionMenu(recording, variable=var, values=values, width=110, dynamic_resizing=False,
                                     fg_color="white", button_color="#DCE5F6", button_hover_color=SELECT,
                                     text_color=INK, dropdown_fg_color=PAPER, dropdown_text_color=INK,
                                     state="disabled" if self.recording else "normal", font=font(12))
            menu.pack(fill="x", padx=3, pady=(0, 4))
            self.options.append(menu)

        services = sections["翻译服务"][1]
        caption(services, "API 优先级")
        order_box = ctk.CTkFrame(services, fg_color=SIDE, corner_radius=8)
        order_box.pack(fill="x", padx=3)
        self.pending_priority = list(self.priority)

        def draw_order():
            for child in order_box.winfo_children():
                child.destroy()
            for index, name in enumerate(self.pending_priority):
                row = ctk.CTkFrame(order_box, fg_color="transparent")
                row.pack(fill="x", padx=9, pady=2)
                label(row, name, 12).pack(side="left", pady=6)
                if index:
                    button(row, "上移", lambda i=index: move(i), 42).pack(side="right")

        def move(index):
            order = self.pending_priority
            order[index - 1], order[index] = order[index], order[index - 1]
            draw_order()

        draw_order()
        label(services, "从上到下尝试，跳过没有密钥的服务。", 11, MUTED).pack(anchor="w", padx=3, pady=5)
        self.entries = {}
        for name, account in (
            ("Kimi Code 订阅 Key", "kimi_key"), ("DeepSeek API Key", "deepseek_key"),
            ("腾讯云 AppID", "tencent_appid"), ("腾讯云 SecretId", "tencent_id"), ("腾讯云 SecretKey", "tencent_key"),
        ):
            caption(services, name)
            entry = ctk.CTkEntry(services, show="*", border_width=1, border_color=BORDER, fg_color="white", text_color=INK)
            entry.pack(fill="x", padx=3, pady=(0, 3))
            try:
                entry.insert(0, keyring.get_password(SERVICE, account) or "")
            except Exception:
                entry.configure(state="disabled")
            self.entries[account] = entry

        appearance = sections["外观"][1]
        caption(appearance, "悬浮球")
        ctk.CTkSwitch(appearance, text="显示桌面悬浮球", variable=self.float_enabled, command=self.update_overlay,
                      text_color=SUB, font=font(12), progress_color=ACCENT).pack(anchor="w", padx=3, pady=5)
        caption(appearance, "窗口透明度")
        slider = ctk.CTkSlider(appearance, from_=.55, to=1., width=150, button_color=ACCENT, progress_color="#A6B9DF",
                               button_hover_color=SUB, command=self.set_opacity)
        slider.set(self.opacity)
        slider.pack(anchor="w", padx=3, pady=(4, 8))
        self.settings_tab = show
        show("录制")

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
        self.close_settings()
        self.status.set("设置已保存")

    def _select(self, sid, *, update_sidebar=True):
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
        if update_sidebar:
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
        translation = translated or ("本段未取得译文" if final else "翻译中…")
        for text, tag in ((original, "original"), (translation, "translation")):
            for part in re.split(r"([A-Za-z][A-Za-z0-9 ,.'’:/()!?-]*)", text):
                if part:
                    tags = (tag, "english") if part[0].isascii() and part[0].isalpha() else (tag,)
                    box.insert("end-1c", part, tags)
            box.insert("end-1c", "\n", tag)
        if final:
            box.mark_set("draft", "end-1c")
        box.configure(state="disabled")
        if scroll and at_bottom:
            box.see("end")

    def select_session(self, sid):
        self.close_settings()
        self.close_export()
        self.history_action = self.project_form = None
        self.status.set("")
        self._select(sid)

    def new_session(self, project_id=None):
        if self.recording:
            self.status.set("请先点击头像暂停当前录制")
            return
        self.session_id = None
        self.draft_project = project_id
        if project_id is not None:
            self.expanded_projects.add(project_id)
        self.draft_title = ""
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
        self.close_export()
        self.status.set("已分别导出原文和译文")

    def toggle(self):
        if self.closing:
            return
        if self.recording:
            self._pause_capture()
            return
        realtime = self.mode.get() == "腾讯云 实时"
        candidates = ["腾讯云"] if realtime else self.priority
        try:
            keys = {key: keyring.get_password(SERVICE, key) for name in candidates for key in KEYS[name]}
            routes = [name for name in candidates if all(keys[key] for key in KEYS[name])]
            missing = not routes
        except Exception:
            self.status.set("无法读取 Windows 凭据，请检查设置")
            return
        if missing or self.device.get() not in self.devices:
            self.status.set("请在「翻译服务」填写密钥" if missing else "请选择可用的麦克风")
            if self.settings_page is None:
                self.toggle_settings()
            self.settings_tab("翻译服务" if missing else "录制")
            return
        run = RecordingRun(self.history_store, keys, routes)
        try:
            self.stream = sd.RawInputStream(samplerate=SAMPLE_RATE, channels=1, dtype="int16",
                                           blocksize=FRAME_SAMPLES, device=self.devices[self.device.get()], callback=run.capture)
            run.capture_started = time.time()
            self.stream.start()
            if self.view_id is None:
                sid = self.history_store.begin(routes[0], self.draft_title or None, self.draft_project)
                self.draft_title = ""
            else:
                sid = self.view_id
                self.history_store.resume(sid)
        except Exception as exc:
            self._close_stream()
            self.status.set(f"无法开始录制 · {exc}")
            return
        run.session_id = sid
        prior = next((r for r in reversed(self.pending_runs) if r.session_id == sid), None)
        run.previous_saved = prior.saved if prior else None
        self.pending_runs.append(run)
        self.current_run = run
        self.session_id = sid
        self.recording = True
        self.partial = ("", "")
        self.status.set("正在收音" if realtime else "正在收音 · 停顿 1 秒后翻译")
        self._select(sid)
        self.update_overlay()
        if self.settings_page is not None:
            for menu in self.options:
                menu.configure(state="disabled")
        backend = AudioBackend.tencent_loop if realtime else priority_loop
        threading.Thread(target=run.process, args=(backend,), daemon=True).start()

    def _pause_capture(self):
        run = self.current_run
        ended = time.time()
        try:
            self._close_stream()
        finally:
            run.stop_event.set()
            self.current_run = None
            self.recording = False
            self.history_store.end(run.session_id, ended)
            self._header_state()
            self._dock_state()
            if self.view_id == run.session_id and not self.has_text:
                self.empty_title.configure(text="已暂停收音")
                self.empty_hint.configure(text="收到的译文会保存在本次会话")
            self.status.set("已暂停收音 · 剩余译文在后台保存")
            if self.settings_page is not None:
                for menu in self.options:
                    menu.configure(state="normal")

    def _close_stream(self):
        if self.stream is not None:
            stream, self.stream = self.stream, None
            try:
                stream.stop()
            finally:
                stream.close()

    def _receive(self, run, e):
        sid = run.session_id
        if e[0] in ("source", "pair", "final_pair"):
            if e[0] == "source":
                start, end, src, dst = time.time(), None, e[1], ""
            else:
                _, start, end, src, dst, _ = e
            if not (src or dst):
                return
            final = e[0] == "final_pair"
            if sid == self.session_id:
                self.partial = ("", "") if final else (src, dst)
            if final:
                self.history_store.append(sid, start, end, src, dst)
            if self.view_id == sid:
                self._write_segment(start, end, src, dst, final=final)
        elif e[0] == "status":
            if any(word in e[1] for word in ("失败", "中断", "错误", "overflow")):
                run.failure = e[1]
            if self.current_run is run:
                self.status.set(e[1])
        elif e[0] == "finished":
            if self.current_run is run:
                self._pause_capture()
            self.pending_runs.remove(run)
            run.route_keys.clear()
            run.saved.set()  # Previous translations have reached SQLite before continuation.
            if not self.recording and self.view_id == sid:
                self.partial = ("", "")
                self._select(sid, update_sidebar=False)
                self.status.set(run.failure or ("已暂停收音 · 剩余译文在后台保存" if self.pending_runs else "已暂停 · 已保存"))

    def poll(self):
        for run in list(self.pending_runs):
            for _ in range(64):
                try:
                    event = run.events.get_nowait()
                except queue.Empty:
                    break
                self._receive(run, event)
                if event[0] == "finished":
                    break
        if not self.export_results.empty():
            self.exporting_project = False
            self.status.set(self.export_results.get_nowait())
        if self.closing and not self.pending_runs and not self.exporting_project:
            self.destroy()
        else:
            self.after(80, self.poll)

    def close(self):
        self.closing = True
        if self.recording:
            self._pause_capture()
        if self.pending_runs:
            self._dock_state()
            self.status.set("已停止收音 · 完成剩余翻译后自动退出")
        elif self.exporting_project:
            self.status.set("完成项目导出后自动退出")
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
