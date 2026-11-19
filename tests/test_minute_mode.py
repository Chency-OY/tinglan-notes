import queue
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import httpx
import numpy as np

from interpreter import FRAME_SAMPLES, SAMPLE_RATE
from routes import MINUTE_BYTES, speech_batches, priority_loop, translate_text


def buffered_app(pcm):
    app = SimpleNamespace(audio=queue.SimpleQueue(), events=queue.SimpleQueue(), stop_event=threading.Event(),
                          session_id=7, capture_started=100., route_keys={}, active_routes=["DeepSeek"],
                          history_store=Mock(), previous_saved=None)
    for pos in range(0, len(pcm), FRAME_SAMPLES * 2):
        app.audio.put(pcm[pos:pos + FRAME_SAMPLES * 2])
    app.stop_event.set()
    return app


class SpeechBatchTests(unittest.TestCase):
    def setUp(self):
        # Deterministic labels isolate the cut timing from VAD's acoustic model.
        vad = patch("routes.webrtcvad.Vad")
        vad.start().return_value.is_speech.side_effect = lambda frame, rate: any(frame)
        self.addCleanup(vad.stop)

    def test_quiet_speech_short_pauses_and_exact_tail(self):
        # Quiet speech below the old gate and a mid-sentence pause must all survive.
        samples = np.full(125 * SAMPLE_RATE + 17, 150, dtype="<i2")
        samples[15*SAMPLE_RATE:15*SAMPLE_RATE + SAMPLE_RATE//2] = 0
        pcm = samples.tobytes()
        batches = list(speech_batches(buffered_app(pcm)))
        self.assertEqual([p for _, p in batches], [0, MINUTE_BYTES, 2 * MINUTE_BYTES])
        self.assertEqual(b"".join(data for data, _ in batches), pcm)

    def test_pause_at_59_seconds_keeps_every_sample_once(self):
        samples = np.full(61 * SAMPLE_RATE, 1500, dtype="<i2")
        samples[58*SAMPLE_RATE:59*SAMPLE_RATE] = 0
        pcm = samples.tobytes()
        batches = list(speech_batches(buffered_app(pcm)))
        self.assertEqual([offset for _, offset in batches], [0, 59 * SAMPLE_RATE * 2])
        self.assertEqual(b"".join(data for data, _ in batches), pcm)

    def test_pause_submits_while_mic_is_open(self):
        pcm = b"\x10\x01" * (2*SAMPLE_RATE) + bytes(2*SAMPLE_RATE)
        app = buffered_app(pcm)
        app.stop_event.clear()
        batches = speech_batches(app)
        self.assertEqual(next(batches), (pcm, 0))
        app.stop_event.set()
        self.assertEqual(list(batches), [])

    def test_long_silence_and_trailing_speech(self):
        speech = b"\x12\x01" * SAMPLE_RATE
        gap = bytes(8 * SAMPLE_RATE)
        batches = list(speech_batches(buffered_app(speech + gap + speech)))
        self.assertEqual(len(batches), 2)
        self.assertEqual(batches[0], (speech + bytes(2*SAMPLE_RATE), 0))
        tail, start = batches[-1]
        self.assertTrue(tail.endswith(speech))
        self.assertEqual(start + len(tail), 2*len(speech) + len(gap))

    def test_context_and_timestamps_while_audio_arrives_during_translation(self):
        first = b"\x11\x01" * (60 * SAMPLE_RATE)
        app = buffered_app(first)
        app.stop_event.clear()
        app.history_store.last_translation.return_value = ("上一次", "Earlier")
        contexts = []
        def translate(client, pcm, routes, keys, notify, previous):
            contexts.append(previous)
            if len(contexts) == 1:
                app.audio.put(b"\x20\x01" * (3 * SAMPLE_RATE))
                app.stop_event.set()
                return "胰岛素", "insulin"
            return "它", "it"
        with patch("routes.translate_segment", side_effect=translate):
            priority_loop(app)
        self.assertEqual(contexts, [("上一次", "Earlier"), ("胰岛素", "insulin")])
        pairs = []
        while not app.events.empty():
            event = app.events.get()
            if event[0] == "final_pair":
                pairs.append(event)
        self.assertEqual([(e[1],e[2],e[5]) for e in pairs], [(100.,160.,7),(160.,163.,7)])

    def test_empty_exact_minute_and_digital_silence(self):
        self.assertEqual(list(speech_batches(buffered_app(b""))), [])
        with patch("routes.translate_segment") as translate:
            priority_loop(buffered_app(bytes(MINUTE_BYTES)))
            translate.assert_not_called()

    def test_latest_turn_and_context_are_separate(self):
        captured = {}
        def reply(request):
            import json
            captured.update(json.loads(request.content))
            return httpx.Response(200, json={"choices":[{"message":{"content":"它结合受体。"}}]})
        with httpx.Client(transport=httpx.MockTransport(reply)) as client:
            translate_text(client,"Kimi 订阅","fixture","It binds the receptor.",("Insulin","胰岛素"))
        self.assertEqual([m["content"] for m in captured["messages"][1:]],
                         ["Insulin","胰岛素","It binds the receptor."])
