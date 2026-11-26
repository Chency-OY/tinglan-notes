"""A compact title strip with Windows drag, resize and window commands."""
from __future__ import annotations

import ctypes
import tkinter as tk
from ctypes import wintypes
from functools import cache
from tkinter import font as tkfont


@cache
def _window_api():
    user32 = ctypes.WinDLL("user32")
    user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
    user32.GetAncestor.restype = wintypes.HWND
    user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.GetWindowLongW.restype = wintypes.LONG
    user32.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.LONG]
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsIconic.argtypes = [wintypes.HWND]
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                   ctypes.c_int, ctypes.c_int, wintypes.UINT]
    user32.SetWindowPos.restype = wintypes.BOOL
    return user32


def main_frame_handle(window):
    frame = int(str(window.tk.call("wm", "frame", window._w)), 0)
    return _window_api().GetAncestor(frame, 2)  # GA_ROOT: outside Tk's client child.


def show_main_on_taskbar(hwnd, *, activate=False):
    if not hwnd:
        return False
    user32 = _window_api()
    exstyle = user32.GetWindowLongW(hwnd, -20)
    taskbar_style = (exstyle | 0x00040000) & ~0x00000080  # WS_EX_APPWINDOW without WS_EX_TOOLWINDOW.
    if taskbar_style != exstyle:
        user32.SetWindowLongW(hwnd, -20, taskbar_style)
    if user32.IsIconic(hwnd):
        if activate:
            user32.ShowWindow(hwnd, 9)  # SW_RESTORE, keeping the prior restore/maximize position.
    elif not user32.IsWindowVisible(hwnd):
        # Explicitly show the main frame even if the process inherited a hidden launch state.
        user32.SetWindowPos(hwnd, None, 0, 0, 0, 0, 0x0057)  # SHOWWINDOW; keep size, position and focus.
    if activate:
        user32.SetForegroundWindow(hwnd)
    return bool(user32.IsWindowVisible(hwnd))


def soften_window_frame(hwnd):
    dwm = ctypes.WinDLL("dwmapi")
    dwm.DwmSetWindowAttribute.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
    # Use the Windows corner radius and a border close to the app palette.
    for attribute, setting in ((33, 2), (34, 0x00EDE4D7)):
        value = wintypes.DWORD(setting)
        dwm.DwmSetWindowAttribute(hwnd, attribute, ctypes.byref(value), ctypes.sizeof(value))


class WindowsTitleStrip(tk.Canvas):
    BACKGROUND = "#F1F4FA"
    INK = "#44516A"
    HOVER = "#E3EBF8"
    RESIZE_EDGES = (
        ("n", "size_ns"), ("s", "size_ns"), ("w", "size_we"), ("e", "size_we"),
        ("nw", "size_nw_se"), ("ne", "size_ne_sw"), ("sw", "size_ne_sw"), ("se", "size_nw_se"),
    )

    @classmethod
    def install(cls, window, scale):
        strip = cls(window, scale)
        try:
            if strip._install_native_frame():
                return strip
        except (AttributeError, OSError, ValueError, tk.TclError):
            pass
        strip.destroy()
        return None

    def __init__(self, window, scale):
        self.scale = scale
        self.bar_height = round(38 * scale)
        self.control_width = round(44 * scale)
        self.resize_border = max(7, round(7 * scale))
        self.resize_grip = round(15 * scale)
        self.resize_handles = {}
        self.edge_drag = None
        self.edges_visible = False
        super().__init__(window, height=self.bar_height, background=self.BACKGROUND, highlightthickness=0, borderwidth=0)
        self.caption_font = tkfont.Font(root=window, family="Microsoft YaHei UI",
                                        size=-round(15 * scale), weight="bold")
        self.hovered = None
        self.pressed = self.drag_origin = None
        self.hwnd = None
        self._install_bindings()

    def _install_bindings(self):
        self.bind("<Configure>", self._draw)
        self.bind("<Motion>", self._motion)
        self.bind("<Leave>", self._leave)
        self.bind("<ButtonPress-1>", self._press)
        self.bind("<B1-Motion>", self._drag)
        self.bind("<ButtonRelease-1>", self._release)
        self.bind("<Double-Button-1>", self._double_click)

    def _install_native_frame(self):
        user32 = self.user32 = ctypes.WinDLL("user32")
        self.comctl = ctypes.windll.comctl32
        result = ctypes.c_ssize_t
        pointer = ctypes.c_size_t
        signed = ctypes.c_ssize_t
        proc = ctypes.WINFUNCTYPE(result, wintypes.HWND, wintypes.UINT, pointer, signed, pointer, pointer)
        self.callback = proc(self._dispatch)
        self.subclass_id = id(self)
        user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        user32.GetWindowRect.restype = wintypes.BOOL
        user32.IsZoomed.argtypes = [wintypes.HWND]
        user32.IsZoomed.restype = wintypes.BOOL
        user32.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
        user32.GetMonitorInfoW.restype = wintypes.BOOL
        user32.MonitorFromRect.argtypes = [ctypes.POINTER(wintypes.RECT), wintypes.DWORD]
        user32.MonitorFromRect.restype = wintypes.HANDLE
        user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, pointer, signed]
        user32.PostMessageW.restype = wintypes.BOOL
        user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                       ctypes.c_int, ctypes.c_int, wintypes.UINT]
        user32.SetWindowPos.restype = wintypes.BOOL
        self.comctl.SetWindowSubclass.argtypes = [wintypes.HWND, proc, pointer, pointer]
        self.comctl.SetWindowSubclass.restype = wintypes.BOOL
        self.comctl.RemoveWindowSubclass.argtypes = [wintypes.HWND, proc, pointer]
        self.comctl.RemoveWindowSubclass.restype = wintypes.BOOL
        self.comctl.DefSubclassProc.argtypes = [wintypes.HWND, wintypes.UINT, pointer, signed]
        self.comctl.DefSubclassProc.restype = result

        # Tk's child window lives inside the mapped Windows frame that owns the system caption.
        hwnd = main_frame_handle(self.master)
        if not hwnd or not self.comctl.SetWindowSubclass(hwnd, self.callback, self.subclass_id, 0):
            return False
        self.hwnd = hwnd
        if not user32.SetWindowPos(hwnd, None, 0, 0, 0, 0, 0x0037):
            self.comctl.RemoveWindowSubclass(hwnd, self.callback, self.subclass_id)
            self.hwnd = None
            return False
        soften_window_frame(hwnd)
        return True

    def _dispatch(self, hwnd, message, wparam, lparam, _subclass, _context):
        if message == 0x0083:  # WM_NCCALCSIZE
            if self.user32.IsZoomed(hwnd):
                self._limit_maximized_client(lparam)
            return 0
        if message == 0x0082:  # WM_NCDESTROY
            self.comctl.RemoveWindowSubclass(hwnd, self.callback, self.subclass_id)
            self.hwnd = None
        return self.comctl.DefSubclassProc(hwnd, message, wparam, lparam)

    def _limit_maximized_client(self, address):
        class MonitorInfo(ctypes.Structure):
            _fields_ = [("size", wintypes.DWORD), ("monitor", wintypes.RECT),
                        ("work", wintypes.RECT), ("flags", wintypes.DWORD)]

        if not address:
            return
        bounds = ctypes.cast(address, ctypes.POINTER(wintypes.RECT)).contents
        monitor = self.user32.MonitorFromRect(ctypes.byref(bounds), 2)
        info = MonitorInfo(ctypes.sizeof(MonitorInfo))
        if self.user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
            bounds.left = max(bounds.left, info.work.left)
            bounds.top = max(bounds.top, info.work.top)
            bounds.right = min(bounds.right, info.work.right)
            bounds.bottom = min(bounds.bottom, info.work.bottom)

    def install_resize_handles(self, surface_color, sidebar_color):
        self.surface_color = surface_color
        self.sidebar_color = sidebar_color
        for edge, cursor in self.RESIZE_EDGES:
            color = self.BACKGROUND if "n" in edge else surface_color
            handle = tk.Canvas(self.master, bg=color, bd=0, highlightthickness=0, cursor=cursor, takefocus=False)
            handle.bind("<ButtonPress-1>", lambda event, side=edge: self._start_resize(event, side))
            handle.bind("<B1-Motion>", self._resize_window)
            handle.bind("<ButtonRelease-1>", self._finish_resize)
            self.resize_handles[edge] = handle
        self.resize_handles["se"].bind("<Configure>", self._draw_resize_grip)
        self.master.bind("<Configure>", self._sync_resize_handles, add="+")
        self._sync_resize_handles()

    def set_sidebar_visible(self, visible):
        if self.resize_handles:
            color = self.sidebar_color if visible else self.surface_color
            for edge in ("w", "sw"):
                self.resize_handles[edge].configure(bg=color)

    def _sync_resize_handles(self, event=None):
        if (event is not None and event.widget is not self.master) or not self.hwnd:
            return
        visible = not self.user32.IsZoomed(self.hwnd)
        if visible == self.edges_visible:
            return
        self.edges_visible = visible
        if not visible:
            for handle in self.resize_handles.values():
                handle.place_forget()
            return
        band, grip = self.resize_border, self.resize_grip
        positions = {
            "n": dict(x=band, y=0, relwidth=1, width=-2 * band, height=band),
            "s": dict(x=band, rely=1, anchor="sw", relwidth=1, width=-band - grip, height=band),
            "w": dict(x=0, y=band, relheight=1, height=-2 * band, width=band),
            "e": dict(relx=1, y=band, anchor="ne", relheight=1, height=-band - grip, width=band),
            "nw": dict(width=band, height=band),
            "ne": dict(relx=1, anchor="ne", width=band, height=band),
            "sw": dict(rely=1, anchor="sw", width=band, height=band),
            "se": dict(relx=1, rely=1, anchor="se", width=grip, height=grip),
        }
        for edge, handle in self.resize_handles.items():
            handle.place(**positions[edge])
            handle.tk.call("raise", handle._w)

    def _draw_resize_grip(self, event):
        grip = event.widget
        grip.delete("all")
        end = event.width - max(3, round(3 * self.scale))
        for size in (3, 6):
            reach = round(size * self.scale)
            grip.create_line(end - reach, end, end, end - reach, fill="#8396BB", width=max(1, round(self.scale)))

    def _start_resize(self, event, edge):
        if not self.hwnd or self.user32.IsZoomed(self.hwnd):
            return
        bounds = wintypes.RECT()
        if self.user32.GetWindowRect(self.hwnd, ctypes.byref(bounds)):
            self.edge_drag = edge, event.x_root, event.y_root, bounds
            event.widget.grab_set()

    def _resize_window(self, event):
        if self.edge_drag is None or not self.hwnd or self.user32.IsZoomed(self.hwnd):
            return
        edge, start_x, start_y, bounds = self.edge_drag
        dx, dy = event.x_root - start_x, event.y_root - start_y
        left, top, right, bottom = bounds.left, bounds.top, bounds.right, bounds.bottom
        min_width, min_height = map(int, self.master.tk.call("wm", "minsize", self.master._w))
        if "w" in edge:
            left = min(right - min_width, left + dx)
        if "e" in edge:
            right = max(left + min_width, right + dx)
        if "n" in edge:
            top = min(bottom - min_height, top + dy)
        if "s" in edge:
            bottom = max(top + min_height, bottom + dy)
        self.user32.SetWindowPos(self.hwnd, None, left, top, right - left, bottom - top, 0x0014)

    def _finish_resize(self, event):
        # A desktop can coalesce motion events, so use the release point too.
        self._resize_window(event)
        self.edge_drag = None
        if event.widget.grab_current() is event.widget:
            event.widget.grab_release()

    def _button(self, x):
        index = (self.winfo_width() - 1 - x) // self.control_width
        return index if 0 <= index < 3 else None

    def _motion(self, event):
        hover = self._button(event.x)
        if hover != self.hovered:
            self.hovered = hover
            self._draw()

    def _leave(self, _event):
        self.hovered = None
        self._draw()

    def _press(self, event):
        self.pressed = self._button(event.x)
        self.drag_origin = None
        if self.pressed is None and self.hwnd:
            self.drag_origin = (event.x_root, event.y_root)
            self.drag_offset = None
            self.grab_set()

    def _end_drag(self):
        self.drag_origin = None
        if self.grab_current() is self:
            self.grab_release()

    def _drag(self, event):
        if self.drag_origin is None or not self.hwnd:
            return
        x, y = self.drag_origin
        if self.drag_offset is None:
            if abs(event.x_root - x) + abs(event.y_root - y) < 4:
                return
            rect = wintypes.RECT()
            if not self.user32.GetWindowRect(self.hwnd, ctypes.byref(rect)):
                return
            dx, dy = x - rect.left, y - rect.top
            if self.user32.IsZoomed(self.hwnd):
                ratio = max(0., min(1., dx / max(1, rect.right - rect.left)))
                _window_api().ShowWindow(self.hwnd, 9)  # Restore before moving.
                if self.user32.GetWindowRect(self.hwnd, ctypes.byref(rect)):
                    dx = round(ratio * (rect.right - rect.left))
            self.drag_offset = dx, dy
        dx, dy = self.drag_offset
        self.user32.SetWindowPos(self.hwnd, None, event.x_root - dx, event.y_root - dy, 0, 0, 0x0015)

    def _double_click(self, event):
        if self._button(event.x) is None and self.hwnd:
            self._end_drag()
            self._toggle_maximize()
        else:
            self._press(event)

    def _toggle_maximize(self):
        command = 0xF120 if self.user32.IsZoomed(self.hwnd) else 0xF030
        self.user32.PostMessageW(self.hwnd, 0x0112, command, 0)

    def _release(self, event):
        self._drag(event)
        self._end_drag()
        index, self.pressed = self.pressed, None
        if index is None or index != self._button(event.x) or not self.hwnd:
            return
        if index == 0:
            self.user32.PostMessageW(self.hwnd, 0x0010, 0, 0)  # WM_CLOSE
        elif index == 1:
            self._toggle_maximize()
        else:
            self.user32.PostMessageW(self.hwnd, 0x0112, 0xF020, 0)

    def _draw(self, _event=None):
        self.delete("all")
        scale = self.scale
        width, height = self.winfo_width(), self.winfo_height()
        middle = height / 2
        self.create_line(0, height - 1, width, height - 1, fill="#E3E9F3")
        self.create_oval(14 * scale, middle - 10 * scale, 34 * scale, middle + 10 * scale,
                         fill="#DCE8FC", outline="")
        for i, line_height in enumerate((5, 10, 7)):
            x = (20 + i * 4) * scale
            self.create_line(x, middle - line_height * scale / 2, x, middle + line_height * scale / 2,
                             fill="#5073B3", width=max(1, round(1.5 * scale)), capstyle="round")
        x = 43 * scale
        for character in self.master.title():
            self.create_text(x, middle, text=character, font=self.caption_font, fill="#293955", anchor="w")
            x += self.caption_font.measure(character) + max(1, round(self.scale))

        maximized = bool(self.hwnd and self.user32.IsZoomed(self.hwnd))
        stroke = max(1, round(scale))
        for index in range(3):
            edge = width - index * self.control_width
            x = edge - self.control_width / 2
            color = self.INK
            if self.hovered == index:
                background = "#F4DBE0" if index == 0 else self.HOVER
                color = "#893A4B" if index == 0 else self.INK
                self.create_rectangle(edge - self.control_width + 3 * scale, 4 * scale,
                                      edge - 3 * scale, height - 4 * scale, fill=background, outline="")
            radius = 4.25 * scale
            if index == 2:
                self.create_line(x - radius, middle, x + radius, middle, fill=color, width=stroke)
            elif index == 0:
                self.create_line(x - radius, middle - radius, x + radius, middle + radius, fill=color, width=stroke)
                self.create_line(x - radius, middle + radius, x + radius, middle - radius, fill=color, width=stroke)
            elif maximized:
                self.create_rectangle(x - radius + 2 * scale, middle - radius - 2 * scale,
                                      x + radius + 2 * scale, middle + radius - 2 * scale, outline=color, width=stroke)
                self.create_rectangle(x - radius, middle - radius, x + radius,
                                      middle + radius, fill=self.HOVER if self.hovered == index else self.BACKGROUND,
                                      outline=color, width=stroke)
            else:
                self.create_rectangle(x - radius, middle - radius, x + radius, middle + radius, outline=color, width=stroke)
