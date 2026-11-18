"""Gesture regressions using the actual, mapped Tk recording control."""
import tkinter as tk
import unittest

from ctk_app import RecordOrb


class RecordingGestureTests(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.addCleanup(self.root.destroy)
        photo = tk.PhotoImage(master=self.root, width=60, height=60)
        class Icon:
            def create_scaled_photo_image(self, scale, mode):
                return photo
        self.calls = []
        self.moves = []
        self.orb = RecordOrb(self.root, [Icon(), Icon()], 1, "white",
                             lambda: self.calls.append("toggle"),
                             lambda x, y: self.moves.append((x, y)))
        self.orb.pack()
        self.root.update()

    def pointer(self, event, x=100, timestamp=1000):
        self.orb.event_generate(event, x=15, y=15, rootx=x, rooty=100, time=timestamp)

    def test_short_click_toggles_only_once_on_release(self):
        self.pointer("<ButtonPress-1>")
        self.assertFalse(self.calls)
        self.pointer("<ButtonRelease-1>", timestamp=1100)
        self.pointer("<ButtonRelease-1>", timestamp=1110)
        self.assertEqual(self.calls, ["toggle"])

    def test_short_drag_returning_to_start_does_not_toggle(self):
        self.pointer("<ButtonPress-1>")
        self.pointer("<B1-Motion>", x=102, timestamp=1050)
        self.pointer("<ButtonRelease-1>", timestamp=1100)
        self.assertTrue(self.moves)
        self.assertFalse(self.calls)

    def test_release_displacement_and_long_press_do_not_toggle(self):
        self.pointer("<ButtonPress-1>")
        self.pointer("<ButtonRelease-1>", x=108, timestamp=1100)
        self.pointer("<ButtonPress-1>")
        self.pointer("<ButtonRelease-1>", timestamp=1500)
        self.assertFalse(self.calls)
