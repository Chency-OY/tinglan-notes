"""Single-window conversation workspace for the Windows interpreter."""
from __future__ import annotations

import queue
import re
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import customtkinter as ctk
import keyring
import sounddevice as sd
from PIL import Image, ImageDraw, ImageFont, ImageOps

from interpreter import FRAME_SAMPLES, SAMPLE_RATE, SERVICE, HistoryStore, Interpreter as AudioBackend, clock_text, safe_filename
from routes import KEYS, priority_loop, priority_order
from window_chrome import WindowsTitleStrip, main_frame_handle, show_main_on_taskbar

APP_TITLE = "中英同传字幕"
PAPER = "#FAFBFE"
SIDE = "#EEF2FB"
INK = "#303B59"
SUB = "#596884"
MUTED = "#657490"
ACCENT = "#6278AC"
SELECT = "#DDE6F8"
BORDER = "#DEE5F2"
SCROLL_TRACK = "#E6ECF7"
SCROLL_THUMB = "#788FBE"
SCROLL_HOVER = "#536D9E"
ROW_HOVER = "#E4EBF8"
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


def monitor_work_area(window, position=None):
    """Return the nearest monitor's usable rectangle in screen pixels."""
    if sys.platform != "win32":
        return 0, 0, window.winfo_screenwidth(), window.winfo_screenheight()
    import ctypes
    from ctypes import wintypes

    class MonitorInfo(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("monitor", wintypes.RECT),
                    ("work", wintypes.RECT), ("flags", wintypes.DWORD)]

    user32 = ctypes.windll.user32
    if position is None:
        user32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
        user32.MonitorFromWindow.restype = wintypes.HANDLE
        monitor = user32.MonitorFromWindow(window.winfo_id(), 2)
    else:
        user32.MonitorFromPoint.argtypes = [wintypes.POINT, wintypes.DWORD]
        user32.MonitorFromPoint.restype = wintypes.HANDLE
        monitor = user32.MonitorFromPoint(wintypes.POINT(*position), 2)
    user32.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.POINTER(MonitorInfo)]
    user32.GetMonitorInfoW.restype = wintypes.BOOL
    info = MonitorInfo()
    info.cbSize = ctypes.sizeof(info)
    if user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
        return info.work.left, info.work.top, info.work.right, info.work.bottom
    return 0, 0, window.winfo_screenwidth(), window.winfo_screenheight()


def move_overlay_window(window, x, y):
    if sys.platform != "win32":
        window.geometry(f"+{max(0, x)}+{max(0, y)}")
        return
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
    user32.GetAncestor.restype = wintypes.HWND
    user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                   ctypes.c_int, ctypes.c_int, wintypes.UINT]
    user32.SetWindowPos.restype = wintypes.BOOL
    hwnd = user32.GetAncestor(window.winfo_id(), 2)
    user32.SetWindowPos(hwnd, None, x, y, 0, 0, 0x0015)  # Keep size, stacking order and focus.


def font(size, bold=False, family=FONT):
    return ctk.CTkFont(family=family, size=size, weight="bold" if bold else "normal")


def label(parent, text, size=12, color=INK, bold=False, **kwargs):
    return ctk.CTkLabel(parent, text=text, height=0, font=font(size, bold), text_color=color, **kwargs)


def button(parent, text, command, width=32, *, symbol=False, fill="transparent", anchor="center"):
    return ctk.CTkButton(parent, text=text, command=command, width=width, height=32,
                         fg_color=fill, hover_color=SELECT, text_color=SUB, corner_radius=8,
                         anchor=anchor, font=font(16 if symbol else 12, family="Segoe MDL2 Assets" if symbol else FONT))


def translation_indicator(enabled):
    image = Image.new("RGBA", (40, 40))
    draw = ImageDraw.Draw(image)
    bounds = (2, 2, 37, 37)
    if enabled:
        draw.ellipse(bounds, fill="#27935E")
    else:
        draw.ellipse(bounds, outline="#8794A6", width=4)
    return ctk.CTkImage(light_image=image, size=(10, 10))


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

    def __init__(self, parent, orbs, scale, background, command, on_drag=None, on_drop=None):
        size = round(60 * scale)
        super().__init__(parent, width=size, height=size, bg=background, borderwidth=0,
                         highlightthickness=0, takefocus=on_drag is None, cursor="hand2")
        self.images = [orb.create_scaled_photo_image(scale, "light") for orb in orbs]
        self.face = self.create_image(0, 0, anchor="nw", image=self.images[0])
        self.command = command
        self.on_drag = on_drag
        self.on_drop = on_drop
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
        self.window_origin = (self.winfo_rootx(), self.winfo_rooty())
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
        if self.dragged:
            if self.on_drop is not None:
                self.on_drop()
        elif elapsed_ms < 400 and time.monotonic() - self.down_time < .40:
            self.invoke()


class OverlayCaption(ctk.CTkFrame):
    """Show one translated segment in a static, wrapped card."""

    WIDTH = 460

    def __init__(self, parent, chroma, scale):
        fill = "#F9FAFE"
        super().__init__(parent, fg_color=fill, bg_color=chroma, border_width=1,
                         border_color=BORDER, corner_radius=14)
        self.scale = scale
        self.pad_x = round(16 * scale)
        self.pad_y = round(10 * scale)
        self.body = tk.Label(self, bg=fill, fg=INK, anchor="nw", justify="left",
                             borderwidth=0, padx=0, pady=0)

    def show(self, text, width, max_height, y):
        text_width = max(1, width - 2 * self.pad_x)
        self.body.configure(text=text, wraplength=text_width)
        for size in range(16, 11, -1):
            self.body.configure(font=(FONT, -round(size * self.scale)))
            height = self.body.winfo_reqheight() + 2 * self.pad_y
            if height <= max_height:
                break
        height = min(height, max_height)
        self.configure(width=width / self.scale, height=height / self.scale)
        self.body.place(x=self.pad_x, y=self.pad_y, width=text_width,
                        height=max(1, height - 2 * self.pad_y))
        self.place(x=0, y=y / self.scale)
        return height


class RecordingRun:
    """Capture and worker state for one uninterrupted microphone interval."""
    capture = AudioBackend.capture

    def __init__(self, history_store, keys, routes, translation_enabled):
        self.history_store = history_store
        self.route_keys = keys
        self.active_routes = routes
        self.translation_enabled = translation_enabled
        self.audio = queue.SimpleQueue()
        self.events = queue.SimpleQueue()
        self.stop_event = threading.Event()
        self.saved = threading.Event()
        self.previous_saved = None
        self.session_id = None
        self.capture_started = 0.
        self.failure = ""

    def process(self):
        try:
            priority_loop(self)
        except Exception as exc:
            self.events.put(("status", f"录制中断 · {type(exc).__name__}"))
        finally:
            self.events.put(("finished",))


@dataclass
class CaptureStart:
    sid: int | None
    project_id: int | None
    title: str
    source: str
    translation_enabled: bool
    cancelled: threading.Event = field(default_factory=threading.Event)
    result: queue.SimpleQueue = field(default_factory=queue.SimpleQueue)


class InterpreterWindow(ctk.CTk):

    def __init__(self, history_store=None):
        ctk.set_appearance_mode("light")
        super().__init__(fg_color=PAPER)
        self.withdraw()
        self.title(APP_TITLE)
        self.iconbitmap(str(ASSETS / "icon.ico"))
        self.geometry("800x460")
        self.minsize(540, 300)
        self.attributes("-topmost", False)
        self.attributes("-alpha", 1.0)
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.history_store = history_store or HistoryStore()
        self.priority = priority_order(self.history_store.preference("priority", ""))
        self.float_enabled = ctk.BooleanVar(value=self.history_store.preference("overlay", "1") == "1")
        self.overlay_caption_enabled = self.history_store.preference("overlay_caption", "1") == "1"
        self.overlay = self.overlay_caption = None
        self.overlay_menu = None
        self.overlay_recording_controls = ()
        self.overlay_subtitle = ""
        self.overlay_captions = {"zh": "", "en": ""}
        self.overlay_caption_language = self.history_store.preference("overlay_caption_language", "en")
        if self.overlay_caption_language not in self.overlay_captions:
            self.overlay_caption_language = "en"
        self.ball_position = self._load_ball_position()
        self.overlay_alpha_percent = self._bounded_preference("overlay_alpha", 90, 55, 100)
        self.draft_title = ""
        self.session_id = self.view_id = None
        self.recording = self.closing = False
        self.panel = None
        self.expanded_projects = set()
        self.recent_open = True
        self.row_menu = self.menu_anchor = None
        self.menu_key = None
        self.menu_row_updates = {}
        self.history_rows = {}
        self.session_sections = {}
        self.archive_rows = {}
        self.archive_row_updates = {}
        self.draft_project = None
        self.settings_page = self.export_page = self.archive_list = None
        self.expanded_archived_projects = set()
        self.export_results = queue.Queue()
        self.exporting_project = False
        self.desktop_folder = desktop_folder()
        self.export_folder = ctk.StringVar(value=str(self.desktop_folder))
        old_zoom = self._bounded_preference("subtitle_zoom", 100, 70, 180)
        self.subtitle_size = self._bounded_preference("subtitle_size", round(11 * old_zoom / 100), 8, 24)
        self.size_readout = ctk.StringVar(value=f"{self.subtitle_size} pt")
        self.overlay_alpha_label = ctk.StringVar(value=f"{self.overlay_alpha_percent}%")
        self.stream = None
        self.start_request = None
        self.current_run = None
        self.pending_runs = []
        self.partial = ("", "")
        self.devices = {f"{i} · {d['name']}": i for i, d in enumerate(sd.query_devices()) if d["max_input_channels"] > 0}
        default_input = sd.default.device[0]
        preferred = next((name for name, index in self.devices.items() if index == default_input), None)
        self.device = ctk.StringVar(value=preferred or next(iter(self.devices), "无可用麦克风"))
        self.status = ctk.StringVar(value="")
        portrait = Image.open(ASSETS / "icon.png").convert("RGBA")
        self.orbs = [orb_image(portrait, active) for active in (False, True)]
        self.wave = wave_image()
        self.folder_icons = (project_icon(), project_icon(opened=True))
        self.sidebar_font = font(12)
        self.sidebar_icons = {glyph: sidebar_glyph(glyph) for glyph in ("\ue70f", "\ue713", "\ue7b8", "\ue8c8", "\ue710", "\ue896", "\ue74d", "\ue8b7", "\ue72b")}
        self.grid_columnconfigure(1, weight=1)
        self.side = ctk.CTkFrame(self, width=224, corner_radius=0, fg_color=SIDE)
        self.side.pack_propagate(False)
        self.title_strip = WindowsTitleStrip.install(self, self.side._get_widget_scaling()) if sys.platform == "win32" else None
        self.content_row = int(self.title_strip is not None)
        self.grid_rowconfigure(self.content_row, weight=1)
        if self.title_strip is not None:
            self.title_strip.grid(row=0, column=0, columnspan=2, sticky="ew")
        self.main = ctk.CTkFrame(self, corner_radius=0, fg_color=PAPER)
        self.main.grid(row=self.content_row, column=1, sticky="nsew")
        self.main.grid_columnconfigure(0, weight=1)
        self.main.grid_rowconfigure(1, weight=1)
        self._build_header()
        self._build_feed()
        self.capture_sources = {"mic": "麦克风"}
        if sys.platform == "win32":
            self.capture_sources = {"both": "内外同时", "system": "系统声音", **self.capture_sources}
        default_source = next(iter(self.capture_sources))
        source = self.history_store.preference("capture_source", default_source)
        self.capture_source = source if source in self.capture_sources else default_source
        self.translate_recording = self.history_store.preference("translate_recording", "1") == "1"
        self.source_setting = tk.StringVar(self, self.capture_sources[self.capture_source])
        self.translation_icons = {state: translation_indicator(state) for state in (False, True)}
        self._build_dock()
        self._select(None)
        self.bind("<Control-MouseWheel>", self._zoom)
        self.set_panel("history")
        if self.title_strip is not None:
            self.title_strip.install_resize_handles(PAPER, SIDE)
            self.title_strip.set_sidebar_visible(True)
        self.bind("<Escape>", self._escape)
        self.bind("<Button-1>", self._outside_row_menu, add="+")
        self.bind("<MouseWheel>", self._scroll_outside_row_menu, add="+")
        self.bind("<Configure>", self._resize_row_menu, add="+")
        self.bind("<Control-b>", lambda _: self.toggle_history())
        self.deiconify()
        if sys.platform == "win32":
            self.after_idle(self._ensure_taskbar_window)
        self.after_idle(self.update_overlay)
        self.after(80, self.poll)

    def _ensure_taskbar_window(self):
        if not self.closing:
            show_main_on_taskbar(main_frame_handle(self))

    def _build_header(self):
        bar = ctk.CTkFrame(self.main, fg_color="transparent")
        bar.grid(row=0, column=0, sticky="ew", padx=14, pady=(12, 0))
        self.history_toggle = button(bar, "", self.toggle_history, 34)
        self.history_toggle.configure(image=sidebar_icon(), corner_radius=7)
        self.history_toggle.pack(side="left", padx=(0, 10))
        self.settings_btn = button(bar, "\ue713", self.toggle_settings, 30, symbol=True)
        self.settings_btn.pack(side="right")
        self.export_btn = button(bar, "导出", self.toggle_export, 36)
        self.export_btn.pack(side="right", padx=(3, 4))
        sizes = ctk.CTkFrame(bar, fg_color="#EDF2FB", corner_radius=8)
        sizes.pack(side="right", padx=(6, 0))
        self.size_down = button(sizes, "−", lambda: self.set_subtitle_size(self.subtitle_size - 1), 23)
        self.size_down.configure(height=26, border_spacing=0, font=font(15, bold=True, family="Segoe UI"), text_color=INK)
        self.size_down.pack(side="left")
        ctk.CTkLabel(sizes, textvariable=self.size_readout, width=36, height=26,
                     font=font(11), text_color=SUB).pack(side="left")
        self.size_up = button(sizes, "+", lambda: self.set_subtitle_size(self.subtitle_size + 1), 23)
        self.size_up.configure(height=26, border_spacing=0, font=font(15, bold=True, family="Segoe UI"), text_color=INK)
        self.size_up.pack(side="left")
        self._size_button_state()

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
        self.scroll = ctk.CTkScrollbar(self.body, width=13, border_spacing=2,
                                       fg_color=SCROLL_TRACK, button_color=SCROLL_THUMB,
                                       button_hover_color=SCROLL_HOVER, minimum_pixel_length=28,
                                       command=self.transcript.yview)
        self.transcript.configure(yscrollcommand=self.scroll.set)
        self._size_fonts()

    def _size_fonts(self):
        box = self.transcript
        for tag, size, color, before, after in (
            ("stamp", max(8, self.subtitle_size - 2), MUTED, 10, 3),
            ("original", self.subtitle_size, SUB, 0, 4),
            ("translation", self.subtitle_size, INK, 0, 12),
        ):
            box.tag_configure(tag, font=(FONT, size, "normal"), foreground=color, spacing1=before, spacing3=after)
        box.tag_configure("english", font=(FONT, self.subtitle_size + 1, "normal"))
        box.tag_raise("english")

    def _zoom(self, event):
        if event.delta:
            self.set_subtitle_size(self.subtitle_size + (1 if event.delta > 0 else -1))
        return "break"

    def _bounded_preference(self, name, default, lower, upper):
        try:
            value = int(self.history_store.preference(name, str(default)))
        except ValueError:
            return default
        return max(lower, min(upper, value))

    def _size_button_state(self):
        self.size_down.configure(state="disabled" if self.subtitle_size == 8 else "normal")
        self.size_up.configure(state="disabled" if self.subtitle_size == 24 else "normal")

    def set_subtitle_size(self, value):
        size = max(8, min(24, value))
        if size != self.subtitle_size:
            self.subtitle_size = size
            self.size_readout.set(f"{size} pt")
            self._size_button_state()
            at_bottom = self.transcript.yview()[1] >= .975
            self._size_fonts()
            if at_bottom:
                self.transcript.see("end")
            self.history_store.set_preference("subtitle_size", str(size))

    def set_overlay_alpha(self, value):
        percent = max(55, min(100, round(value)))
        if percent != self.overlay_alpha_percent:
            self.overlay_alpha_percent = percent
            self.overlay_alpha_label.set(f"{percent}%")
            if self.overlay is not None:
                self.overlay.attributes("-alpha", percent / 100)
            self.history_store.set_preference("overlay_alpha", str(percent))

    def _build_dock(self):
        dock = ctk.CTkFrame(self.main, fg_color="transparent")
        dock.grid(row=2, column=0, sticky="ew", padx=16, pady=(0, 13))
        dock.grid_columnconfigure(0, weight=1)
        self.notice = label(dock, "", 11, ACCENT, anchor="w", textvariable=self.status)
        self.notice.grid(row=0, column=0, columnspan=2, sticky="ew", padx=8, pady=(0, 3))
        strip = ctk.CTkFrame(dock, fg_color="transparent")
        strip.grid(row=1, column=0, sticky="ew", padx=(4, 6))
        self.source_control = self._capture_source_selector(strip)
        self.source_control.pack(anchor="w", pady=(2, 4))
        self.record_options = ctk.CTkFrame(strip, fg_color="transparent")
        self.record_options.pack(anchor="w")
        self.mode_control = self._translation_toggle(self.record_options)
        self.mode_control.pack(side="left", padx=(0, 8))
        self.action = label(self.record_options, "", 11, SUB, anchor="w")
        self.action.pack(side="left")
        self.live_link = button(self.record_options, "回到录制", lambda: self.select_session(self.session_id),
                                68, fill=SELECT)
        self.orb = RecordOrb(dock, self.orbs, self.main._get_widget_scaling(), PAPER, self.toggle)
        self.orb.grid(row=1, column=1, padx=3, pady=3)

    def _capture_controls_state(self):
        return "disabled" if self.recording or self.start_request or self.closing else "normal"

    def _capture_source_selector(self, parent):
        return ctk.CTkSegmentedButton(
            parent, values=list(self.capture_sources.values()), variable=self.source_setting,
            command=self._set_capture_source, state=self._capture_controls_state(),
            font=font(12), height=29, corner_radius=9, fg_color="#EEF2FB",
            selected_color=SELECT, selected_hover_color="#CCDAF4",
            unselected_color="#EEF2FB", unselected_hover_color="#E5ECF9",
            text_color=INK, text_color_disabled=MUTED)

    def _translation_toggle(self, parent):
        return ctk.CTkButton(
            parent, text="翻译", font=font(12), image=self.translation_icons[self.translate_recording],
            command=self._toggle_translation, state=self._capture_controls_state(),
            compound="left", anchor="w", width=58, height=26, corner_radius=6, border_spacing=4,
            fg_color="transparent", hover_color="#EEF2FB", text_color=INK, text_color_disabled=MUTED)

    def _set_capture_source(self, value):
        self.capture_source = next(source for source, label in self.capture_sources.items() if label == value)
        self.history_store.set_preference("capture_source", self.capture_source)

    def _toggle_translation(self):
        self.translate_recording = not self.translate_recording
        icon = self.translation_icons[self.translate_recording]
        self.mode_control.configure(image=icon)
        if self.overlay_recording_controls:
            self.overlay_recording_controls[1].configure(image=icon)
        self.history_store.set_preference("translate_recording", str(int(self.translate_recording)))

    def _dock_state(self):
        state = self._capture_controls_state()
        for control in (self.source_control, self.mode_control, *self.overlay_recording_controls):
            control.configure(state=state)
        text = "点击头像暂停" if self.recording else "点击头像继续" if self.view_id else "点击头像录制"
        starting = self.start_request is not None and not self.start_request.cancelled.is_set()
        if self.start_request is not None:
            text = "再次点击可取消" if starting else "正在取消启动"
        self.action.configure(text=text)
        active = self.recording or starting
        self.orb.set_state(active, not self.closing)
        if self.overlay is not None:
            self.float_canvas.set_state(active, not self.closing)
        if self.settings_page is not None:
            for menu in self.options:
                menu.configure(state="disabled" if self.recording or self.start_request else "normal")
        if self.recording and self.view_id != self.session_id:
            self.action.pack_forget()
            self.live_link.pack(side="left")
        else:
            self.live_link.pack_forget()
            if not self.action.winfo_manager():
                self.action.pack(side="left")

    def toggle_history(self):
        self.set_panel(None if self.panel == "history" else "history")

    def _load_ball_position(self):
        saved = self.history_store.preference("overlay_position", "")
        try:
            x, y = map(int, saved.split(","))
            return x, y
        except ValueError:
            return None

    def _remember_ball_position(self):
        if self.ball_position is not None:
            x, y = self.ball_position
            self.history_store.set_preference("overlay_position", f"{x},{y}")

    def close_overlay(self):
        self._close_overlay_menu()
        if self.overlay is not None:
            self.overlay.destroy()
            self.overlay = self.overlay_caption = None

    def hide_overlay(self):
        self.history_store.set_preference("overlay", "0")
        self.float_enabled.set(False)
        self.update_overlay()

    def toggle_overlay_caption(self):
        self.overlay_caption_enabled = not self.overlay_caption_enabled
        self.history_store.set_preference("overlay_caption", str(int(self.overlay_caption_enabled)))
        if self.overlay is not None:
            self._place_overlay(*self.ball_position)

    def _close_overlay_menu(self):
        if self.overlay_menu is not None:
            menu, self.overlay_menu = self.overlay_menu, None
            self.overlay_recording_controls = ()
            menu.destroy()

    def _show_overlay_menu(self, event):
        if self.closing:
            return
        self._close_overlay_menu()
        surface = "#FFFFFF"
        chroma = "#FF00FF" if sys.platform == "win32" else surface
        menu = self.overlay_menu = tk.Toplevel(self.overlay, background=chroma)
        menu.withdraw()
        menu.overrideredirect(True)
        menu.attributes("-topmost", True)
        if sys.platform == "win32":
            menu.attributes("-toolwindow", True)
            menu.attributes("-transparentcolor", chroma)

        card = ctk.CTkFrame(menu, bg_color=chroma, fg_color=surface, border_color=BORDER,
                            border_width=1, corner_radius=14)
        card.pack(fill="both", expand=True)
        settings = ctk.CTkFrame(card, fg_color="#F7F9FD", corner_radius=10)
        settings.pack(fill="x", padx=7, pady=(7, 2))
        label(settings, "录制音源", 11, SUB, anchor="w").pack(anchor="w", padx=8, pady=(6, 3))
        source_control = self._capture_source_selector(settings)
        source_control.pack(fill="x", padx=8)
        mode_control = self._translation_toggle(settings)
        mode_control.pack(anchor="w", padx=8, pady=(6, 6))
        self.overlay_recording_controls = (source_control, mode_control)

        caption_action = "隐藏字幕" if self.overlay_caption_enabled else "显示字幕"
        language_action = "切换到中文字幕" if self.overlay_caption_language == "en" else "切换到英文字幕"
        choices = ((caption_action, self.toggle_overlay_caption, INK, "#EEF2FB"),
                   (language_action, self._switch_overlay_caption_language, INK, "#EEF2FB"),
                   ("关闭悬浮球", self.hide_overlay, INK, "#EEF2FB"),
                   ("退出", self.close, "#A24460", "#FAEDF2"))

        def activate(command):
            self._close_overlay_menu()
            command()

        controls = []
        for text, command, color, hover in choices:
            control = ctk.CTkButton(card, text=text, command=lambda run=command: activate(run),
                                    width=184, height=38, corner_radius=9, border_spacing=10,
                                    anchor="w", fg_color="transparent", hover_color=hover,
                                    text_color=color, font=font(15))
            control.pack(fill="x", padx=6, pady=(6 if not controls else 2, 4))
            controls.append(control)

        click_targets = [(segment, segment.invoke) for segment in source_control.grid_slaves(row=0)]
        click_targets.append((mode_control, mode_control.invoke))
        click_targets.extend((control, control.invoke) for control in controls)
        selected = None

        def select(step):
            nonlocal selected
            selected = (0 if step > 0 else len(controls) - 1) if selected is None else (selected + step) % len(controls)
            for index, control in enumerate(controls):
                control.configure(fg_color=choices[index][3] if selected == index else "transparent")
            return "break"

        def invoke_selected(_event):
            if selected is not None:
                controls[selected].invoke()
            return "break"

        def dismiss(_event):
            self._close_overlay_menu()
            return "break"

        def handle_press(event):
            if self.overlay_menu is not menu:
                return
            left, top = menu.winfo_rootx(), menu.winfo_rooty()
            if not (left <= event.x_root < left + menu.winfo_width()
                    and top <= event.y_root < top + menu.winfo_height()):
                return dismiss(event)

        def handle_release(event):
            if self.overlay_menu is not menu:
                return
            for control, invoke in click_targets:
                x, y = control.winfo_rootx(), control.winfo_rooty()
                if (x <= event.x_root < x + control.winfo_width()
                        and y <= event.y_root < y + control.winfo_height()):
                    invoke()
                    return "break"

        def dismiss_if_inactive():
            if self.overlay_menu is menu and menu.focus_displayof() is None:
                self._close_overlay_menu()

        menu.bind("<ButtonPress-1>", handle_press)
        menu.bind("<ButtonPress-3>", handle_press)
        menu.bind("<ButtonRelease-1>", handle_release)
        # Route both direct and grabbed clicks through the menu before child widget bindings.
        menu_tag = str(menu)
        for control, _ in click_targets:
            for widget in (control, *control.winfo_children()):
                widget.bindtags((menu_tag, *(tag for tag in widget.bindtags() if tag != menu_tag)))
        # Focus can move within the app before Windows delivers a click to the popup's grab.
        menu.bind("<FocusOut>", lambda _: menu.after_idle(dismiss_if_inactive))
        menu.bind("<Escape>", dismiss)
        menu.bind("<Up>", lambda _: select(-1))
        menu.bind("<Down>", lambda _: select(1))
        menu.bind("<Return>", invoke_selected)

        menu.update_idletasks()
        width, height = menu.winfo_reqwidth(), menu.winfo_reqheight()
        margin = round(8 * card._get_widget_scaling())
        left, top, right, bottom = monitor_work_area(self, (event.x_root, event.y_root))
        x = max(left, min(event.x_root + margin, right - width - margin))
        y = event.y_root + margin
        if y + height > bottom:
            y = max(top, event.y_root - height - margin)
        menu.geometry(f"{width}x{height}+{max(0, x)}+{max(0, y)}")
        menu.deiconify()
        menu.update_idletasks()
        move_overlay_window(menu, x, y)
        menu.focus_force()
        menu.grab_set()

    def _switch_overlay_caption_language(self):
        self.overlay_caption_language = "zh" if self.overlay_caption_language == "en" else "en"
        self.history_store.set_preference("overlay_caption_language", self.overlay_caption_language)
        self._refresh_overlay_subtitle()

    def _show_overlay_translation(self, translated, original="", *, transcript_only=False):
        translated, original = translated.strip(), original.strip()
        if transcript_only:
            self.overlay_captions = {"zh": original, "en": original}
            self._refresh_overlay_subtitle()
            return
        # The translation targets the opposite language. Prefer its language when available.
        sample = translated or original
        han = sum("\u3400" <= char <= "\u9fff" for char in sample)
        latin = sum(char.isascii() and char.isalpha() for char in sample)
        language = "zh" if han and han >= latin else "en"
        other = "en" if language == "zh" else "zh"
        self.overlay_captions = {language: sample, other: original if translated else ""}
        self._refresh_overlay_subtitle()

    def _refresh_overlay_subtitle(self):
        text = self.overlay_captions[self.overlay_caption_language]
        if text == self.overlay_subtitle:
            return
        self.overlay_subtitle = text
        if self.overlay is not None and self.overlay_caption_enabled:
            self._place_overlay(*self.ball_position)

    def _place_overlay(self, x, y):
        win = self.overlay
        caption = self.overlay_caption
        scale = caption.scale
        size = self.float_canvas.winfo_reqwidth()
        left, top, right, bottom = monitor_work_area(self, (x, y))
        x = max(left, min(right - size, x))
        width = height = size
        caption_y = size + round(6 * scale)
        if self.overlay_caption_enabled and self.overlay_subtitle and bottom - top > caption_y:
            width = min(round(caption.WIDTH * scale), right - left)
            height = caption_y + caption.show(self.overlay_subtitle, width, bottom - top - caption_y, caption_y)
        else:
            caption.place_forget()
        base = max(left, min(right - width, x + (size - width) // 2))
        displayed_y = max(top, min(bottom - height, y))
        self.float_canvas.place(x=x - base, y=0)
        win.geometry(f"{width}x{height}")
        move_overlay_window(win, base, displayed_y)
        return x, max(top, min(bottom - size, y))

    def update_overlay(self):
        if not self.float_enabled.get():
            self.close_overlay()
            return
        if self.overlay is not None:
            return
        chroma = "#FF00FF"
        win = self.overlay = tk.Toplevel(self, background=chroma, takefocus=False)
        win.withdraw()
        win.title("录制悬浮球")
        win.overrideredirect(True)
        if sys.platform == "win32":
            win.attributes("-toolwindow", True)
            win.attributes("-transparentcolor", chroma)
        win.attributes("-alpha", self.overlay_alpha_percent / 100)
        win.attributes("-topmost", True)
        scale = self.main._get_widget_scaling()
        size = round(60 * scale)
        self.overlay_caption = OverlayCaption(win, chroma, scale)

        def move_to(x, y):
            self.ball_position = self._place_overlay(x, y)

        def click():
            if self.closing:
                return
            was_recording = self.recording
            self.toggle()
            if was_recording:
                self.deiconify()
                self.lift()

        self.float_canvas = RecordOrb(win, self.orbs, scale, chroma, click, move_to, self._remember_ball_position)
        self.float_canvas.bind("<ButtonRelease-3>", self._show_overlay_menu)
        self.float_canvas.set_state(self.recording or self.start_request is not None, not self.closing)
        if self.ball_position is None:
            left, top, right, bottom = monitor_work_area(self)
            margin = round(12 * scale)
            self.ball_position = max(left, right - size - margin), max(top, bottom - size - margin)
        self.ball_position = self._place_overlay(*self.ball_position)
        win.deiconify()
        win.update_idletasks()
        self._place_overlay(*self.ball_position)

    def toggle_settings(self):
        if self.settings_page is not None:
            self.close_settings()
            return
        self.close_export()
        self._close_row_menu()
        self.settings_page = ctk.CTkFrame(self.main, corner_radius=0, fg_color=PAPER)
        self.settings_page.grid(row=0, column=0, rowspan=2, sticky="nsew")
        self.settings_page.lift()
        self._settings(self.settings_page)

    def close_settings(self):
        if self.settings_page is not None:
            self._close_row_menu()
            self.settings_page.destroy()
            self.settings_page = self.settings_tab = self.archive_list = None
            self.archive_rows.clear()
            self.archive_row_updates.clear()

    def _escape(self, _event):
        if self.row_menu is not None:
            self._close_row_menu()
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
            self._close_row_menu()
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
        if self.title_strip is not None:
            self.title_strip.set_sidebar_visible(name == "history")
        self._close_row_menu()
        self.history_rows.clear()
        self.session_sections.clear()
        self.menu_row_updates.clear()
        for child in self.side.winfo_children():
            child.destroy()
        if name is None:
            self.history_toggle.pack(side="left", before=self.title_box, padx=(0, 10))
            self.side.grid_remove()
            return
        self.history_toggle.pack_forget()
        self.side.grid(row=self.content_row, column=0, sticky="ns")
        row = ctk.CTkFrame(self.side, fg_color="transparent")
        row.pack(fill="x", padx=(15, 9), pady=(10, 4))
        label(row, "会话" if self.title_strip is not None else "中英同传", 13, bold=True).pack(side="left")
        close = button(row, "", lambda: self.set_panel(None), 28)
        close.configure(image=sidebar_icon())
        close.pack(side="right")
        self._sidebar_button(self.side, "新会话", self.new_session, icon="\ue70f").pack(fill="x", padx=10)
        footer = ctk.CTkFrame(self.side, fg_color="transparent")
        footer.pack(side="bottom", fill="x", padx=10, pady=(1, 6))
        self._sidebar_button(footer, "设置", self.toggle_settings, icon="\ue713").pack(side="left")
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
        self._close_row_menu()
        canvas = self.listing._parent_canvas
        position = canvas.yview()[0]
        for child in self.listing.winfo_children():
            child.destroy()
        self.history_rows.clear()
        self.session_sections.clear()
        self.menu_row_updates.clear()
        projects = dict(self.history_store.projects(archived=False))
        grouped = {}
        for sid, started, title, pid, archived in self.history_store.library_sessions():
            if not archived:
                grouped.setdefault(pid, []).append((sid, started, title))

        heading = ctk.CTkFrame(self.listing, fg_color="transparent")
        heading.pack(fill="x", pady=(3, 0))
        label(heading, "项目", 11, MUTED, bold=True).pack(side="left", padx=7)
        add = button(heading, "创建项目", lambda: self._project_form(add), 60)
        add.pack(side="right")
        for pid, name in projects.items():
            opened = pid in self.expanded_projects
            self._history_row(
                self.listing, ("project", pid), name, lambda n=pid: self._toggle_project(n),
                lambda row, n=pid: self._project_menu(row, n),
                lambda row, n=pid, title=name: self._project_form(row, pid=n, name=title),
                icon=self.folder_icons[opened],
            )
            if opened:
                self._session_section(pid, grouped.get(pid, []))

        self._sidebar_button(self.listing, ("⌄" if self.recent_open else "›") + "  最近会话",
                             self._toggle_recent).pack(fill="x", pady=(8, 0))
        if self.recent_open:
            self._session_section(None, grouped.get(None, []))
        self.listing.update_idletasks()
        canvas.yview_moveto(position)

    def _session_section(self, pid, sessions):
        section = ctk.CTkFrame(self.listing, fg_color="transparent")
        section.pack(fill="x", padx=(18 if pid is not None else 0, 0))
        empty = None
        if not sessions:
            empty = label(section, "暂无会话", 11, MUTED)
            empty.pack(anchor="w", padx=10, pady=3)
        self.session_sections[pid] = (section, empty)
        for sid, started, title in sessions:
            self._session_row(section, sid, started, title, pid)

    def _add_recording_row(self, sid, pid):
        if self.panel != "history" or pid not in self.session_sections:
            return
        section, empty = self.session_sections[pid]
        if empty is not None:
            empty.destroy()
            self.session_sections[pid] = (section, None)
        before = section.pack_slaves()
        _, started, _, _ = self.history_store.session(sid)
        row = self._session_row(section, sid, started, self.history_store.title(sid), pid)
        if before:
            row.pack_configure(before=before[0])

    def _row_selected(self, key):
        kind, identifier = key
        return self.view_id == identifier if kind == "session" else (
            self.view_id is None and self.draft_project == identifier)

    def _history_row(self, parent, key, title, command, menu_command, edit_command, *, icon=None, archive=False):
        background = PAPER if archive else SIDE
        card = ctk.CTkFrame(parent, fg_color="transparent", corner_radius=9)
        card.pack(fill="x", pady=1)
        (self.archive_rows if archive else self.history_rows)[key] = card
        short = title if archive else self._shorten_title(title, 112 - (18 if icon else 0))
        primary = self._sidebar_button(card, short, command, icon)
        primary.configure(height=30, hover=False)
        primary.pack(side="left", fill="x", expand=True, padx=(1, 54))
        actions = ctk.CTkFrame(card, fg_color="transparent")
        more = button(actions, "⋯", lambda: menu_command(card), 25)
        more.configure(height=25, border_spacing=0)
        more.pack(side="left")
        edit = button(actions, "", lambda: edit_command(card), 25)
        edit.configure(image=self.sidebar_icons["\ue70f"], height=25, border_spacing=0)
        edit.pack(side="left", padx=(0, 2))

        shown = None
        hovered = False

        def refresh():
            nonlocal shown
            active = self.menu_anchor is card
            selected = self._row_selected(key)
            state = selected, hovered, active
            if shown == state:
                return
            shown = state
            card.configure(fg_color=SELECT if selected else ROW_HOVER if hovered or active else background)
            if selected or hovered or active:
                actions.place(relx=1, rely=.5, anchor="e")
            else:
                actions.place_forget()

        def enter(_event):
            nonlocal hovered
            hovered = True
            refresh()

        def leave(_event):
            nonlocal hovered
            x, y = card.winfo_pointerxy()
            left, top = card.winfo_rootx(), card.winfo_rooty()
            if not (left <= x < left + card.winfo_width() and top <= y < top + card.winfo_height()):
                hovered = False
                refresh()

        for widget in (card, primary, actions, more, edit):
            widget.bind("<Enter>", enter, add="+")
            widget.bind("<Leave>", leave, add="+")
            widget.bind("<Button-3>", lambda _: menu_command(card), add="+")
        (self.archive_row_updates if archive else self.menu_row_updates)[key] = refresh
        refresh()
        return card

    def _session_row(self, parent, sid, started, title, project, *, archived=False, project_archived=False):
        return self._history_row(
            parent, ("session", sid), title or datetime.fromtimestamp(started).strftime("%m.%d %H:%M"),
            lambda: self.select_session(sid),
            lambda row: self._session_menu(row, sid, project, archived, project_archived),
            lambda _row: self.rename_from_history(sid), archive=archived or project_archived,
        )

    def _project_menu(self, row, pid, *, archived=False):
        actions = []
        if not archived:
            actions.append(("在此新建会话", "\ue710", lambda: self.new_session(pid)))
        actions.extend([
            ("导出项目 ZIP", "\ue896", lambda: self._export_from_menu(pid)),
            ("恢复项目" if archived else "归档项目", "\ue7b8", lambda: self._archive_project(pid, not archived)),
        ])
        self._show_row_menu(row, ("project", pid), actions)

    def _export_from_menu(self, pid):
        self._close_row_menu()
        self._export_project(pid)

    def _session_menu(self, row, sid, pid, archived, project_archived=False):
        actions = [
            ("重命名", "\ue70f", lambda: self.rename_from_history(sid)),
            ("移入项目   ›", "\ue8b7", lambda: self._move_menu(row, sid, pid, archived, project_archived)),
            ("导出 TXT", "\ue896", lambda: self._export_session(sid)),
        ]
        if not project_archived:
            actions.append(("恢复会话" if archived else "归档", "\ue7b8", lambda: self._archive(sid, not archived)))
        actions.append(("删除", "\ue74d", lambda: self.confirm_delete(row, sid)))
        self._show_row_menu(row, ("session", sid), actions)

    def _move_menu(self, row, sid, current, archived, project_archived):
        choices = [(self._shorten_title(name, 126), "\ue8b7", lambda n=pid: self._move(sid, n))
                   for pid, name in self.history_store.projects(archived=False) if pid != current]
        self._show_row_menu(row, ("move", sid), [
            ("返回", "\ue72b", lambda: self._session_menu(row, sid, current, archived, project_archived)),
            ("创建项目并移入", "\ue710", lambda: self._project_form(row, sid=sid)),
            *([("移出当前项目", "\ue8b7", lambda: self._move(sid, None))] if current is not None else []),
            *choices,
        ])

    def _make_row_menu(self, anchor, key):
        self._close_row_menu()
        menu = self.row_menu = ctk.CTkFrame(self, fg_color="#FFFFFF", bg_color=PAPER, border_width=1,
                                           border_color="#D5DFF0", corner_radius=10)
        self.menu_anchor, self.menu_key = anchor, key
        self._refresh_row_selection()
        return menu

    def _show_row_menu(self, anchor, key, actions):
        if self.menu_anchor is anchor and self.menu_key == key:
            self._close_row_menu()
            return
        menu = self._make_row_menu(anchor, key)
        body = menu
        if len(actions) > 6:
            body = ctk.CTkScrollableFrame(menu, width=160, height=176, fg_color="transparent",
                                          scrollbar_button_color=SCROLL_THUMB)
            body.pack(fill="both", padx=4, pady=4)
        for text, icon, command in actions:
            control = self._sidebar_button(body, text, command, icon)
            if text in ("删除", "确认删除"):
                control.configure(text_color="#A24460")
            control.configure(width=158, height=30, hover_color="#EDF2FA", corner_radius=6)
            control.pack(fill="x", padx=5, pady=(2, 1))
        self._position_row_menu()

    def _position_row_menu(self):
        menu, row = self.row_menu, self.menu_anchor
        if menu is None or not row.winfo_viewable():
            return
        menu.update_idletasks()
        if self.row_menu is not menu:
            return
        width, height = menu.winfo_reqwidth(), menu.winfo_reqheight()
        x = row.winfo_rootx() - self.winfo_rootx() + row.winfo_width()
        top = row.winfo_rooty() - self.winfo_rooty()
        bottom = top + row.winfo_height()
        right = self.winfo_width() - width - 7
        y = top if x <= right else (bottom if bottom + height <= self.winfo_height() else top - height)
        x = max(6, min(x + 4, right))
        y = max(6, min(y, self.winfo_height() - height - 6))
        scale = menu._get_widget_scaling()
        menu.place(x=x / scale, y=y / scale)
        menu.lift()

    def _close_row_menu(self):
        if self.row_menu is not None:
            menu = self.row_menu
            self.row_menu = self.menu_anchor = self.menu_key = None
            menu.destroy()
            self._refresh_row_selection()

    def _refresh_row_selection(self):
        for rows in (self.menu_row_updates, self.archive_row_updates):
            for refresh in rows.values():
                refresh()

    @staticmethod
    def _within(widget, parent):
        return parent is not None and (widget == parent or str(widget).startswith(str(parent) + "."))

    def _outside_row_menu(self, event):
        if self.row_menu is None or not event.widget.winfo_exists():
            return
        if not (self._within(event.widget, self.row_menu) or self._within(event.widget, self.menu_anchor)):
            self._close_row_menu()

    def _scroll_outside_row_menu(self, event):
        if self.row_menu is not None and not self._within(event.widget, self.row_menu):
            self._close_row_menu()

    def _resize_row_menu(self, event):
        if event.widget is self and self.row_menu is not None:
            self._close_row_menu()

    def _toggle_project(self, pid):
        self.expanded_projects.symmetric_difference_update({pid})
        self._history_list()

    def _toggle_recent(self):
        self.recent_open = not self.recent_open
        self._history_list()

    def _draw_archive(self):
        if self.archive_list is None:
            return
        self._close_row_menu()
        self.archive_rows.clear()
        self.archive_row_updates.clear()
        for child in self.archive_list.winfo_children():
            child.destroy()
        projects = dict(self.history_store.projects(archived=True))
        grouped = {pid: [] for pid in projects}
        loose = []
        for sid, started, title, pid, archived in self.history_store.library_sessions():
            item = (sid, started, title, pid)
            if pid in projects:
                grouped[pid].append(item)
            elif archived:
                loose.append(item)
        if not projects and not loose:
            label(self.archive_list, "暂无归档项目或会话", 12, MUTED).pack(anchor="w", padx=6, pady=12)
            return
        if projects:
            label(self.archive_list, "项目", 11, MUTED, bold=True).pack(anchor="w", padx=6, pady=(6, 4))
        for pid, name in projects.items():
            opened = pid in self.expanded_archived_projects
            self._history_row(
                self.archive_list, ("project", pid), name, lambda n=pid: self._toggle_archived_project(n),
                lambda row, n=pid: self._project_menu(row, n, archived=True),
                lambda row, n=pid, title=name: self._project_form(row, pid=n, name=title),
                icon=self.folder_icons[opened], archive=True,
            )
            if opened:
                section = ctk.CTkFrame(self.archive_list, fg_color="transparent")
                section.pack(fill="x", padx=(18, 0))
                if not grouped[pid]:
                    label(section, "暂无会话", 11, MUTED).pack(anchor="w", padx=6)
                for sid, started, title, project in grouped[pid]:
                    self._session_row(section, sid, started, title, project, project_archived=True)
        if loose:
            label(self.archive_list, "会话", 11, MUTED, bold=True).pack(anchor="w", padx=6, pady=(8, 4))
        for sid, started, title, pid in loose:
            self._session_row(self.archive_list, sid, started, title, pid, archived=True)

    def _toggle_archived_project(self, pid):
        self.expanded_archived_projects.symmetric_difference_update({pid})
        self._draw_archive()

    def _archive_project(self, pid, archive):
        self.history_store.archive_project(pid, archive)
        self._close_row_menu()
        if archive and self.draft_project == pid:
            self.draft_project = None
        if not archive:
            self.expanded_archived_projects.discard(pid)
            self.expanded_projects.add(pid)
        self._history_list()
        self._draw_archive()
        self.status.set("项目已归档，可在「设置 → 已归档」恢复" if archive else "项目已恢复")

    def _archive(self, sid, archive):
        self.history_store.archive_session(sid, archive)
        self._close_row_menu()
        self._history_list()
        self._draw_archive()
        self.status.set("会话已归档" if archive else "会话已恢复")

    def _move(self, sid, pid):
        self.history_store.move_session(sid, pid)
        if pid is not None:
            self.expanded_projects.add(pid)
        else:
            self.recent_open = True
        self._close_row_menu()
        self._history_list()
        self._draw_archive()
        self.status.set("已移入项目" if pid is not None else "已移出项目")

    def _project_form(self, anchor, *, sid=None, pid=None, name=""):
        form = self._make_row_menu(anchor, ("project-form", pid or sid))
        label(form, "项目名称", 11, MUTED).pack(anchor="w", padx=10, pady=(8, 2))
        entry = ctk.CTkEntry(form, width=177, fg_color="white", border_color=BORDER)
        entry.insert(0, name)
        entry.pack(fill="x", padx=8, pady=4)
        feedback = label(form, "", 10, "#A24460")
        feedback.pack(anchor="w", padx=8)

        def save():
            try:
                if pid is None:
                    project = self.history_store.create_project(entry.get())
                else:
                    self.history_store.rename_project(pid, entry.get())
                    project = pid
            except ValueError as exc:
                feedback.configure(text=str(exc))
                self._position_row_menu()
                return
            if sid is not None:
                self._move(sid, project)
                return
            self._close_row_menu()
            if pid is None:
                self.expanded_projects.add(project)
            self._history_list()
            self._draw_archive()
            self.status.set("项目已创建" if pid is None else "项目名称已保存")

        entry.bind("<Return>", lambda _: save())
        controls = ctk.CTkFrame(form, fg_color="transparent")
        controls.pack(fill="x", padx=6, pady=(0, 6))
        button(controls, "保存" if pid is not None else "创建", save, 52, fill=SELECT).pack(side="right")
        button(controls, "取消", self._close_row_menu, 38).pack(side="right", padx=3)
        self._position_row_menu()
        entry.focus_set()
        entry.select_range(0, "end")

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

    def _session_in_use(self, sid):
        return any(run.session_id == sid for run in self.pending_runs) or (
            self.start_request is not None and self.start_request.sid == sid)

    def confirm_delete(self, row, sid):
        if self._session_in_use(sid):
            self.status.set("请等这段录制结束并保存后再删除")
            return
        self._show_row_menu(row, ("delete", sid), [
            ("确认删除", "\ue74d", lambda: self.delete_session(sid)),
            ("取消", "\ue72b", self._close_row_menu),
        ])

    def delete_session(self, sid):
        if self._session_in_use(sid):
            return
        self._close_row_menu()
        self.history_store.delete_session(sid)
        if self.session_id == sid:
            self.session_id = None
        if self.view_id == sid:
            self.draft_title = ""
            self.draft_project = None
            self.close_export()
            self._select(self.session_id)
        self._history_list()
        self._draw_archive()
        self.status.set("会话已删除")

    def _settings(self, page):
        header = ctk.CTkFrame(page, fg_color="transparent")
        header.pack(fill="x", padx=18, pady=(9, 4))
        self._page_heading(header, "设置", self.close_settings)
        button(header, "保存设置", self.save_settings, 72, fill=SELECT).pack(side="right")
        tabs = ctk.CTkFrame(page, fg_color="transparent")
        tabs.pack(fill="x", padx=18, pady=(0, 4))
        sections = {}
        for name in ("录制", "翻译服务", "外观", "已归档"):
            frame = ctk.CTkScrollableFrame(page, fg_color="transparent", scrollbar_button_color=BORDER)
            tab = button(tabs, name, lambda n=name: show(n), 61)
            tab.pack(side="left", padx=(0, 3))
            sections[name] = (tab, frame)
        self.archive_list = sections["已归档"][1]

        def show(name):
            self._close_row_menu()
            for key, (tab, frame) in sections.items():
                tab.configure(fg_color=SELECT if key == name else "transparent")
                if key == name:
                    frame.pack(fill="both", expand=True, padx=(18, 12), pady=(0, 4))
                else:
                    frame.pack_forget()
            if name == "已归档":
                self._draw_archive()

        def caption(parent, text):
            label(parent, text, 12, INK, bold=True).pack(anchor="w", padx=3, pady=(5, 3))

        recording = sections["录制"][1]
        caption(recording, "麦克风")
        device_menu = ctk.CTkOptionMenu(
            recording, variable=self.device, values=list(self.devices) or ["无可用麦克风"],
            width=110, dynamic_resizing=False, fg_color="white", button_color="#DCE5F6",
            button_hover_color=SELECT, text_color=INK, dropdown_fg_color=PAPER, dropdown_text_color=INK,
            state="disabled" if self.recording or self.start_request else "normal", font=font(12),
        )
        device_menu.pack(fill="x", padx=3, pady=(0, 4))
        self.options = [device_menu]

        services = sections["翻译服务"][1]
        label(services, "翻译方向 · 中英自动互译", 12, SUB).pack(anchor="w", padx=3, pady=(9, 2))
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

        def slider_row(text, readout, limits, steps, value, change):
            row = ctk.CTkFrame(appearance, fg_color="transparent")
            row.pack(fill="x", padx=3, pady=(7, 1))
            label(row, text, 12, INK, bold=True).pack(side="left")
            label(row, "", 11, MUTED, textvariable=readout).pack(side="right")
            control = ctk.CTkSlider(appearance, from_=limits[0], to=limits[1], number_of_steps=steps,
                                    button_color=ACCENT, progress_color="#A6B9DF", button_hover_color=SUB, command=change)
            control.set(value)
            control.pack(fill="x", padx=3, pady=(4, 7))
            return control

        ctk.CTkSwitch(appearance, text="显示桌面悬浮球", variable=self.float_enabled, command=self.update_overlay,
                      text_color=SUB, font=font(12), progress_color=ACCENT).pack(anchor="w", padx=3, pady=(8, 2))
        slider_row("悬浮球不透明度", self.overlay_alpha_label, (55, 100), 9,
                   self.overlay_alpha_percent, self.set_overlay_alpha)
        self.settings_tab = show
        show("录制")

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
        self.history_store.set_preference("overlay", str(int(self.float_enabled.get())))
        self.close_settings()
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
            self.empty_hint.configure(text="识别到的文字会逐段显示" if self.recording and sid == self.session_id else
                                      "原文与译文将在这里逐段显示" if sid is None else "这段会话还没有已保存的内容")
            self.empty.place(relx=.5, rely=.48, anchor="center")
        self.transcript.yview_moveto(1. if sid == self.session_id else 0.)
        self._refresh_row_selection()
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
        translation = translated or ("" if final else "翻译中…")
        for text, tag in ((original, "original"), (translation, "translation")):
            if not text:
                continue
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
        self._close_row_menu()
        self.status.set("")
        self._select(sid)

    def new_session(self, project_id=None):
        if self.recording or self.start_request is not None:
            self.status.set("请先点击头像暂停录制或取消启动")
            return
        self.session_id = None
        self._show_overlay_translation("")
        self.draft_project = project_id
        if project_id is not None and project_id not in self.expanded_projects:
            self.expanded_projects.add(project_id)
            self._history_list()
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
        if self.start_request is not None:
            self.start_request.cancelled.set()
            self.status.set("正在取消启动…")
            self._dock_state()
            return

        device = self.devices.get(self.device.get())
        if self.capture_source != "system" and device is None:
            self._start_failed("请选择可用的麦克风", "录制")
            return
        request = self.start_request = CaptureStart(self.view_id, self.draft_project, self.draft_title,
                                                    self.capture_source, self.translate_recording)
        self.status.set(f"正在启动录制 · {self.capture_sources[request.source]}")
        self._dock_state()
        candidates = list(self.priority)
        threading.Thread(target=self._open_capture, args=(request, candidates, device),
                         name="audio-start", daemon=True).start()
        self.after(25, self._finish_capture_start)

    def _open_capture(self, request, candidates, device):
        stream = None
        keys, routes = {}, []
        if request.translation_enabled:
            try:
                keys = {key: keyring.get_password(SERVICE, key) for name in candidates for key in KEYS[name]}
            except Exception:
                request.result.put((None, None, "无法读取 Windows 凭据，请检查设置", "翻译服务"))
                return
            routes = [name for name in candidates if all(keys[key] for key in KEYS[name])]
            if not routes:
                request.result.put((None, None, "请在「翻译服务」填写密钥", "翻译服务"))
                return
        if request.cancelled.is_set():
            request.result.put((None, None, None, None))
            return
        run = RecordingRun(self.history_store, keys, routes, request.translation_enabled)
        try:
            def open_microphone(capture, blocksize=FRAME_SAMPLES):
                return sd.RawInputStream(samplerate=SAMPLE_RATE, channels=1, dtype="int16",
                                         blocksize=blocksize, device=device, callback=capture)

            if request.source == "system":
                from system_audio import SystemAudioStream

                stream = SystemAudioStream(run.capture)
            elif request.source == "both":
                from system_audio import MixedAudioStream

                stream = MixedAudioStream(lambda capture: open_microphone(capture, SAMPLE_RATE // 50), run.capture)
            else:
                stream = open_microphone(run.capture)
            if request.cancelled.is_set():
                stream.close()
                request.result.put((None, None, None, None))
                return
            run.capture_started = time.time()
            stream.start()
        except Exception as exc:
            if stream is not None:
                try:
                    stream.close()
                except Exception:
                    pass
            request.result.put((None, None, f"无法开始录制 · {exc}", None))
            return
        request.result.put((run, stream, None, None))

    def _finish_capture_start(self):
        request = self.start_request
        try:
            run, stream, error, tab = request.result.get_nowait()
        except queue.Empty:
            self.after(25, self._finish_capture_start)
            return
        self.start_request = None
        self.stream = stream
        if request.cancelled.is_set() or self.closing:
            try:
                self._close_stream()
            finally:
                self._dock_state()
                self.status.set("已取消启动")
            return
        if error:
            self._start_failed(error, tab)
            return
        sid = request.sid
        new = sid is None
        try:
            if new:
                sid = self.history_store.begin(run.active_routes[0], request.title or None, request.project_id)
                self.draft_title = ""
            else:
                self.history_store.resume(sid)
        except Exception as exc:
            self._close_stream()
            self._start_failed(f"无法保存会话 · {exc}")
            return

        run.session_id = sid
        prior = next((r for r in reversed(self.pending_runs) if r.session_id == sid), None)
        run.previous_saved = prior.saved if prior else None
        self.pending_runs.append(run)
        self.current_run = run
        if self.session_id != sid:
            self._show_overlay_translation("")
        self.session_id = sid
        self.recording = True
        self.partial = ("", "")
        if new:
            self._add_recording_row(sid, request.project_id)
        mode = "同传" if run.translation_enabled else "本地转写"
        self.status.set(f"正在收音 · {mode}")
        if self.view_id == request.sid:
            if new:
                self._select(sid)
            else:
                self._header_state()
                self.transcript.yview_moveto(1)
                if not self.has_text:
                    self.empty_title.configure(text="正在聆听")
                    self.empty_hint.configure(text="识别到的原文和译文会逐段显示" if run.translation_enabled
                                              else "识别到的文字会逐段保存")
        self._dock_state()
        threading.Thread(target=run.process, daemon=True).start()

    def _start_failed(self, error, settings_tab=None):
        self._dock_state()
        self.status.set(error)
        if settings_tab:
            if self.settings_page is None:
                self.toggle_settings()
            self.settings_tab(settings_tab)

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
                self.empty_hint.configure(text="收到的文字会保存在本次会话")
            self.status.set("已暂停收音 · 剩余文字在后台保存")

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
                if sid == self.session_id:
                    self._show_overlay_translation(dst, src, transcript_only=not run.translation_enabled)
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
                self._select(sid)
                self.status.set(run.failure or ("已暂停收音 · 剩余文字在后台保存" if self.pending_runs else "已暂停 · 已保存"))

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
        if self.closing and not self.pending_runs and not self.exporting_project and self.start_request is None:
            self.destroy()
        else:
            self.after(80, self.poll)

    def close(self):
        self.closing = True
        if self.recording:
            self._pause_capture()
        if self.start_request is not None:
            self.start_request.cancelled.set()
            self._dock_state()
            self.status.set("正在停止收音")
        elif self.pending_runs:
            self._dock_state()
            self.status.set("已停止收音 · 保存剩余文字后自动退出")
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
                hwnd = windows.FindWindowW("TkTopLevel", APP_TITLE)
                if hwnd:
                    show_main_on_taskbar(hwnd, activate=True)
                    break
                time.sleep(.1)
        else:
            InterpreterWindow().mainloop()
    finally:
        if handle:
            kernel.CloseHandle(handle)


if __name__ == "__main__":
    main()
