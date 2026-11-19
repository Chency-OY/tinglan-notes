import ctypes
import sys
import tempfile
import tkinter as tk
import unittest
from ctypes import wintypes
from pathlib import Path
from unittest.mock import patch

from ctk_app import InterpreterWindow
from interpreter import HistoryStore
from window_chrome import main_frame_handle, show_main_on_taskbar


@unittest.skipUnless(sys.platform == "win32", "Windows taskbar behavior")
class TaskbarWindowTests(unittest.TestCase):
    def test_startup_and_shortcut_recover_hidden_main_window(self):
        user32 = ctypes.WinDLL("user32")
        user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.IsWindowVisible.argtypes = [wintypes.HWND]
        user32.IsIconic.argtypes = [wintypes.HWND]
        user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
        user32.GetAncestor.restype = wintypes.HWND
        with tempfile.TemporaryDirectory() as folder, patch("ctk_app.sd.query_devices", return_value=[]):
            app = InterpreterWindow(HistoryStore(Path(folder) / "test.db"))
            try:
                hwnd = main_frame_handle(app)
                self.assertTrue(hwnd)
                user32.ShowWindow(hwnd, 0)
                self.assertFalse(user32.IsWindowVisible(hwnd))
                app.update()
                self.assertTrue(user32.IsWindowVisible(hwnd))
                self.assertTrue(user32.GetWindowLongW(hwnd, -20) & 0x00040000)

                overlay = user32.GetAncestor(app.overlay.winfo_id(), 2)
                self.assertTrue(user32.GetWindowLongW(overlay, -20) & 0x00000080)
                self.assertFalse(user32.GetWindowLongW(overlay, -20) & 0x00040000)

                user32.ShowWindow(hwnd, 0)
                self.assertTrue(show_main_on_taskbar(hwnd, activate=True))
                app.update()
                self.assertTrue(user32.IsWindowVisible(hwnd))

                user32.ShowWindow(hwnd, 6)
                self.assertTrue(user32.IsIconic(hwnd))
                show_main_on_taskbar(hwnd)
                self.assertTrue(user32.IsIconic(hwnd))
                show_main_on_taskbar(hwnd, activate=True)
                self.assertFalse(user32.IsIconic(hwnd))
            finally:
                app.close_overlay()
                for ident in app.tk.splitlist(app.tk.call("after", "info")):
                    try:
                        app.after_cancel(ident)
                    except tk.TclError:
                        pass
                app.destroy()


    def test_caption_moves_on_release_and_font_size_stays_visible(self):
        user32 = ctypes.WinDLL("user32")
        user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        with tempfile.TemporaryDirectory() as folder, patch("ctk_app.sd.query_devices", return_value=[]):
            store = HistoryStore(Path(folder) / "test.db")
            store.set_preference("overlay", "0")
            store.set_preference("subtitle_zoom", "130")
            app = InterpreterWindow(store)
            try:
                app.geometry("540x330+70+100")
                app.update()
                strip = app.title_strip
                self.assertIsNotNone(strip)
                before, after = wintypes.RECT(), wintypes.RECT()
                self.assertTrue(user32.GetWindowRect(strip.hwnd, ctypes.byref(before)))
                x, y = 90, 18
                strip.event_generate("<ButtonPress-1>", x=x, y=y, rootx=before.left+x, rooty=before.top+y)
                strip.event_generate("<ButtonRelease-1>", x=x+50, y=y+40, rootx=before.left+x+50, rooty=before.top+y+40)
                self.assertTrue(user32.GetWindowRect(strip.hwnd, ctypes.byref(after)))
                self.assertEqual((after.left, after.top), (before.left+50, before.top+40))
                self.assertTrue(app.size_up.winfo_viewable())
                self.assertEqual(app.size_readout.get(), "14 pt")
                app.size_up.invoke()
                self.assertEqual(app.size_readout.get(), "15 pt")
                self.assertEqual(store.preference("subtitle_size", ""), "15")
                self.assertEqual(int(app.tk.splitlist(app.transcript.tag_cget("original", "font"))[1]), 15)
            finally:
                for ident in app.tk.splitlist(app.tk.call("after", "info")):
                    app.after_cancel(ident)
                app.destroy()
