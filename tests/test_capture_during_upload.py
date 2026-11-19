import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import routes
from ctk_app import InterpreterWindow
from interpreter import HistoryStore, SAMPLE_RATE


class PauseDuringUploadTests(unittest.TestCase):
    def test_capture_pause_and_resume_while_first_request_is_blocked(self):
        entered, release, second_ready = (threading.Event() for _ in range(3))
        cut = []
        uploads = []
        original_batches = routes.speech_batches
        def observe_batches(run):
            for pcm, offset in original_batches(run):
                cut.append(pcm)
                if len(cut) == 2:
                    second_ready.set()
                yield pcm, offset
        def translate(client, pcm, providers, keys, notify, previous):
            uploads.append(pcm)
            if len(uploads) == 1:
                entered.set()
                if not release.wait(12):
                    raise TimeoutError("test upload")
            return f"Class {len(uploads)}", f"课堂 {len(uploads)}"

        with tempfile.TemporaryDirectory() as tmp, patch("ctk_app.sd.query_devices", return_value=[
                {"name":"Test input","max_input_channels":1}]), patch("ctk_app.keyring.get_password",return_value="fixture"), \
                patch("ctk_app.sd.RawInputStream", side_effect=lambda **kw: Mock()), \
                patch("routes.speech_batches", side_effect=observe_batches), \
                patch("routes.translate_segment", side_effect=translate), patch("routes.webrtcvad.Vad") as vad:
            vad.return_value.is_speech.side_effect = lambda frame, rate: frame.startswith(b"B")
            db = HistoryStore(Path(tmp) / "test.db")
            app = InterpreterWindow(db)
            def wait_for(check):
                limit = time.monotonic() + 10
                while not check() and time.monotonic() < limit:
                    app.update()
                    time.sleep(.01)
                self.assertTrue(check())
            try:
                app.toggle()
                run = app.current_run
                sid = run.session_id
                silence = bytes(2*SAMPLE_RATE)
                speech = b"B\x01"*SAMPLE_RATE
                weak = b"a\x00"*SAMPLE_RATE  # The detector misses this quiet second.
                run.capture(speech + silence, 0, None, None)
                wait_for(entered.is_set)
                run.capture(weak + speech + silence, 0, None, None)
                wait_for(second_ready.is_set)
                self.assertTrue(app.recording)
                self.assertEqual(cut, [speech + silence, weak + speech + silence])
                app.orb.invoke()
                self.assertFalse(app.recording)
                self.assertIsNone(app.stream)
                self.assertTrue(app.orb.enabled)
                self.assertIn("已暂停",app.status.get())
                self.assertIsNotNone(db.session(sid)[2])
                app.orb.invoke()
                self.assertTrue(app.recording)
                self.assertIsNone(db.session(sid)[2])
                later = app.current_run
                self.assertIsNot(run, later)
                later.capture(speech,0,None,None)
                release.set()
                wait_for(run.saved.is_set)
                self.assertTrue(app.recording)
                self.assertIs(app.current_run,later)
                app.toggle()
                wait_for(lambda: not app.pending_runs)
                self.assertEqual(uploads,[speech + silence, weak + speech + silence, speech])
                self.assertEqual([r[2] for r in db.segments(sid)],["Class 1","Class 2","Class 3"])
                self.assertTrue(app.transcript.tag_ranges("english"))
                self.assertEqual(app.transcript.tag_cget("original","font"),app.transcript.tag_cget("translation","font"))
                self.assertGreater(int(app.tk.splitlist(app.transcript.tag_cget("english","font"))[1]),
                                   int(app.tk.splitlist(app.transcript.tag_cget("original","font"))[1]))
            finally:
                release.set()
                if app.recording:
                    app.toggle()
                wait_for(lambda: not app.pending_runs)
                for callback in app.tk.splitlist(app.tk.call("after", "info")):
                    app.after_cancel(callback)
                app.destroy()
