import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ctk_app import FONT, InterpreterWindow, monitor_work_area
from interpreter import HistoryStore


class OverlayCaptionTests(unittest.TestCase):
    def test_wrapped_segment_and_persistent_right_click_visibility(self):
        with tempfile.TemporaryDirectory() as folder, patch("ctk_app.sd.query_devices", return_value=[]):
            store = HistoryStore(Path(folder) / "test.db")
            app = InterpreterWindow(store)
            try:
                app.update()
                orb = app.float_canvas
                caption = app.overlay_caption
                self.assertFalse(caption.winfo_ismapped())

                def caption_action(index=0):
                    orb.event_generate("<ButtonRelease-3>", x=10, y=10,
                                       rootx=orb.winfo_rootx() + 10, rooty=orb.winfo_rooty() + 10)
                    app.update()
                    card, = app.overlay_menu.winfo_children()
                    return [button for button in card.winfo_children() if hasattr(button, "invoke")][index]

                def click_caption_action(action, *, move_focus=False):
                    menu = app.overlay_menu
                    if move_focus:
                        app.focus_force()
                        app.update()
                    self.assertIs(app.overlay_menu, menu)
                    # Click the row away from its text, even if the grab targets the popup itself.
                    x = action.winfo_rootx() + action.winfo_width() - 12
                    y = action.winfo_rooty() + action.winfo_height() // 2
                    menu.event_generate("<ButtonPress-1>", rootx=x, rooty=y)
                    self.assertIsNone(app.overlay_menu)

                segment = "This sentence tests subtitle wrapping. " * 14 + "The complete translation stays in one card."
                app._show_overlay_translation(segment)
                app.update()
                body = caption.body
                self.assertEqual(body.cget("text"), segment)
                self.assertGreater(body.winfo_height(), 2 * round(16 * caption.scale))
                self.assertEqual(body.cget("font"), f"{{{FONT}}} {-round(16 * caption.scale)}")
                self.assertEqual(body.winfo_height(), body.winfo_reqheight())
                self.assertGreaterEqual(caption.winfo_rooty(), orb.winfo_rooty() + orb.winfo_height())
                left, top, right, bottom = monitor_work_area(app, app.ball_position)
                self.assertGreaterEqual(app.overlay.winfo_rootx(), left)
                self.assertGreaterEqual(app.overlay.winfo_rooty(), top)
                self.assertLessEqual(app.overlay.winfo_rootx() + app.overlay.winfo_width(), right)
                self.assertLessEqual(app.overlay.winfo_rooty() + app.overlay.winfo_height(), bottom)

                action = caption_action()
                self.assertEqual(action.cget("text"), "隐藏字幕")
                click_caption_action(action, move_focus=True)
                app.update()
                self.assertFalse(caption.winfo_ismapped())
                self.assertTrue(orb.winfo_ismapped())
                self.assertEqual(store.preference("overlay_caption", "1"), "0")

                app._show_overlay_translation("Next translation", "下一段原文")
                app.update()
                self.assertFalse(caption.winfo_ismapped())
                action = caption_action()
                self.assertEqual(action.cget("text"), "显示字幕")
                click_caption_action(action)
                app.update()
                self.assertTrue(caption.winfo_ismapped())
                self.assertEqual(body.cget("text"), "Next translation")
                self.assertEqual(store.preference("overlay_caption", "0"), "1")

                action = caption_action(1)
                self.assertEqual(action.cget("text"), "切换到中文字幕")
                click_caption_action(action)
                app.update()
                self.assertEqual(body.cget("text"), "下一段原文")
                self.assertEqual(store.preference("overlay_caption_language", "en"), "zh")

                app._show_overlay_translation("这是下一段译文", "This is the next original segment.")
                self.assertEqual(body.cget("text"), "这是下一段译文")
                action = caption_action(1)
                self.assertEqual(action.cget("text"), "切换到英文字幕")
                click_caption_action(action)
                app.update()
                self.assertEqual(body.cget("text"), "This is the next original segment.")

                app._show_overlay_translation("")
                app.update()
                self.assertFalse(caption.winfo_ismapped())
            finally:
                app.close_overlay()
                for callback in app.tk.splitlist(app.tk.call("after", "info")):
                    app.after_cancel(callback)
                app.destroy()


    def test_popup_recording_controls_share_main_window_preferences(self):
        with tempfile.TemporaryDirectory() as folder, patch("ctk_app.sd.query_devices", return_value=[]):
            store = HistoryStore(Path(folder) / "test.db")
            app = InterpreterWindow(store)
            try:
                app.update()
                orb = app.float_canvas
                orb.event_generate("<ButtonRelease-3>", x=10, y=10,
                                   rootx=orb.winfo_rootx() + 10, rooty=orb.winfo_rooty() + 10)
                app.update()
                menu = app.overlay_menu
                sources, translation = app.overlay_recording_controls

                def click_via_grab(control):
                    x = control.winfo_rootx() + control.winfo_width() // 2
                    y = control.winfo_rooty() + control.winfo_height() // 2
                    menu.event_generate("<ButtonPress-1>", rootx=x, rooty=y)
                    self.assertIs(app.overlay_menu, menu)

                self.assertEqual(sources.get(), app.source_control.get())
                system = next((item for item in sources.grid_slaves(row=0) if item.cget("text") == "系统声音"),
                              None)
                if system is not None:
                    click_via_grab(system)
                    self.assertEqual(app.source_control.get(), "系统声音")
                    self.assertEqual(store.preference("capture_source", ""), "system")
                    microphone = next(item for item in app.source_control.grid_slaves(row=0)
                                      if item.cget("text") == "麦克风")
                    microphone.invoke()
                    self.assertEqual(sources.get(), "麦克风")

                self.assertEqual(translation.get(), 1)
                # A native click on the label must toggle only once, even with the menu grab.
                switch_label = next(child for child in translation.winfo_children()
                                    if child.winfo_class() == "Label")
                switch_label.event_generate("<ButtonPress-1>", x=1, y=1,
                                            rootx=switch_label.winfo_rootx() + 1,
                                            rooty=switch_label.winfo_rooty() + 1)
                self.assertEqual(app.mode_control.get(), 0)
                self.assertFalse(app.translate_recording)
                self.assertEqual(store.preference("translate_recording", ""), "0")
                click_via_grab(translation)
                self.assertEqual(app.mode_control.get(), 1)
                self.assertTrue(app.translate_recording)

                app.recording = True
                app._dock_state()
                self.assertEqual(sources.cget("state"), "disabled")
                self.assertEqual(translation.cget("state"), "disabled")
                click_via_grab(translation)
                self.assertEqual(app.mode_control.get(), 1)
            finally:
                app.close_overlay()
                for callback in app.tk.splitlist(app.tk.call("after", "info")):
                    app.after_cancel(callback)
                app.destroy()
