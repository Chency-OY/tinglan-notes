"""A compact title strip with Windows drag, resize and window commands."""
from __future__ import annotations

import ctypes
import tkinter as tk
from ctypes import wintypes
from tkinter import font as tkfont


class WindowsTitleStrip(tk.Canvas):
    BACKGROUND = "#F1F4FA"
    INK = "#44516A"
    HOVER = "#E3EBF8"

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
        self.bar_height = round(36 * scale)
        self.control_width = round(44 * scale)
        self.resize_border = max(5, round(5 * scale))
        super().__init__(window, height=self.bar_height, background=self.BACKGROUND, highlightthickness=0, borderwidth=0)
        families = {name.casefold() for name in tkfont.families(window)}
        family = "DengXian" if "dengxian" in families else "等线" if "等线" in families else "Microsoft YaHei UI"
        self.caption_font = tkfont.Font(root=window, family=family, size=-round(15 * scale))
        self.hovered = None
        self.pressed = None
        self.hwnd = None
        self._install_bindings()

    def _install_bindings(self):
        self.bind("<Configure>", self._draw)
        self.bind("<Motion>", self._motion)
        self.bind("<Leave>", self._leave)
        self.bind("<ButtonPress-1>", self._press)
        self.bind("<ButtonRelease-1>", self._release)

    def _install_native_frame(self):
        user32 = self.user32 = ctypes.WinDLL("user32")
        self.comctl = ctypes.windll.comctl32
        result = ctypes.c_ssize_t
        pointer = ctypes.c_size_t
        signed = ctypes.c_ssize_t
        proc = ctypes.WINFUNCTYPE(result, wintypes.HWND, wintypes.UINT, pointer, signed, pointer, pointer)
        self.callback = proc(self._dispatch)
        self.subclass_id = id(self)
        user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
        user32.GetAncestor.restype = wintypes.HWND
        user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        user32.GetClientRect.restype = wintypes.BOOL
        user32.ScreenToClient.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
        user32.ScreenToClient.restype = wintypes.BOOL
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
        window = self.master
        frame = int(str(window.tk.call("wm", "frame", window._w)), 0)
        hwnd = user32.GetAncestor(frame, 2)
        if not hwnd or not self.comctl.SetWindowSubclass(hwnd, self.callback, self.subclass_id, 0):
            return False
        self.hwnd = hwnd
        if not user32.SetWindowPos(hwnd, None, 0, 0, 0, 0, 0x0037):
            self.comctl.RemoveWindowSubclass(hwnd, self.callback, self.subclass_id)
            self.hwnd = None
            return False
        return True

    def _dispatch(self, hwnd, message, wparam, lparam, _subclass, _context):
        if message == 0x0083:  # WM_NCCALCSIZE
            if self.user32.IsZoomed(hwnd):
                self._limit_maximized_client(lparam)
            return 0
        if message == 0x0084:  # WM_NCHITTEST
            result = self._hit_test(hwnd, lparam)
            if result is not None:
                return result
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

    def _hit_test(self, hwnd, position):
        x = ctypes.c_short(position & 0xFFFF).value
        y = ctypes.c_short((position >> 16) & 0xFFFF).value
        point = wintypes.POINT(x, y)
        client = wintypes.RECT()
        if not self.user32.ScreenToClient(hwnd, ctypes.byref(point)) or not self.user32.GetClientRect(hwnd, ctypes.byref(client)):
            return None
        if not (0 <= point.x < client.right and 0 <= point.y < client.bottom):
            return None
        if not self.user32.IsZoomed(hwnd):
            border = self.resize_border
            left = point.x < border
            right = point.x >= client.right - border
            top = point.y < border
            bottom = point.y >= client.bottom - border
            if top:
                return 13 if left else 14 if right else 12
            if bottom:
                return 16 if left else 17 if right else 15
            if left or right:
                return 10 if left else 11
        if point.y < self.bar_height and point.x < client.right - 3 * self.control_width:
            return 2  # HTCAPTION: native drag, double click and system menu.
        return 1      # HTCLIENT: let Tk deliver clicks to the control.

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

    def _release(self, event):
        index, self.pressed = self.pressed, None
        if index is None or index != self._button(event.x) or not self.hwnd:
            return
        if index == 0:
            self.user32.PostMessageW(self.hwnd, 0x0010, 0, 0)  # WM_CLOSE
        elif index == 1:
            command = 0xF120 if self.user32.IsZoomed(self.hwnd) else 0xF030
            self.user32.PostMessageW(self.hwnd, 0x0112, command, 0)
        else:
            self.user32.PostMessageW(self.hwnd, 0x0112, 0xF020, 0)

    def _draw(self, _event=None):
        self.delete("all")
        scale = self.scale
        width, height = self.winfo_width(), self.winfo_height()
        middle = height / 2
        self.create_line(0, height - 1, width, height - 1, fill="#E3E9F3")
        self.create_oval(13 * scale, middle - 8 * scale, 29 * scale, middle + 8 * scale, fill="#DDE8FC", outline="")
        for i, line_height in enumerate((4, 7, 5)):
            x = (18 + i * 3) * scale
            self.create_line(x, middle - line_height * scale / 2, x, middle + line_height * scale / 2,
                             fill="#5876AA", width=max(1, round(scale)), capstyle="round")
        self.create_text(37 * scale, middle, text="中英同传", font=self.caption_font, fill=self.INK, anchor="w")

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
