"""Windows WASAPI loopback capture from the default playback device."""

from contextlib import ExitStack
import queue
import threading
import time

import numpy as np

from interpreter import SAMPLE_RATE


class SystemAudioStream:
    def __init__(self, capture):
        import audioop
        import pyaudiowpatch as audio

        self.backend = audio.PyAudio()
        try:
            device = self.backend.get_default_wasapi_loopback()
            channels = device["maxInputChannels"]
            rate = int(device["defaultSampleRate"])
            conversion_state = None

            def callback(data, frames, timing, flags):
                nonlocal conversion_state
                if channels > 1:
                    samples = np.frombuffer(data, dtype="<i2").reshape(-1, channels)
                    data = samples.mean(axis=1).astype("<i2").tobytes()
                data, conversion_state = audioop.ratecv(data, 2, 1, rate, SAMPLE_RATE, conversion_state)
                capture(data, len(data) // 2, timing, "系统声音输入溢出" if flags & audio.paInputOverflow else "")
                return None, audio.paContinue

            self.stream = self.backend.open(format=audio.paInt16, channels=channels, rate=rate,
                                            input=True, input_device_index=device["index"],
                                            frames_per_buffer=max(1, rate // 50), stream_callback=callback,
                                            start=False)
        except Exception:
            self.backend.terminate()
            raise

    def start(self):
        self.stream.start_stream()

    def stop(self):
        if self.stream.is_active():
            self.stream.stop_stream()

    def close(self):
        try:
            self.stream.close()
        finally:
            self.backend.terminate()



class PcmMixer:
    """Align asynchronous mono inputs on one capture timeline."""

    BLOCK = SAMPLE_RATE // 50
    BUFFER = SAMPLE_RATE // 10

    def __init__(self, capture):
        self.capture = capture
        self.blocks = {}
        self.source_end = {}
        self.output_at = self.end = 0

    def add(self, source, received_at, data, status):
        samples = np.frombuffer(data, dtype="<i2")
        start = received_at - len(samples)
        previous = self.source_end.get(source, start)
        # Both callbacks can jitter. Keep consecutive packets contiguous unless input has a gap.
        start = previous if start < previous + self.BUFFER // 2 else start
        end = start + len(samples)
        self.source_end[source] = end
        self.end = max(self.end, end)
        if status:
            self.capture(b"", 0, None, status)
        pos = max(start, self.output_at)
        while pos < end:
            block_start = pos // self.BLOCK * self.BLOCK
            offset = pos - block_start
            size = min(end - pos, self.BLOCK - offset)
            block = self.blocks.get(block_start)
            if block is None:
                block = self.blocks[block_start] = np.zeros(self.BLOCK, dtype=np.int32)
            block[offset:offset + size] += samples[pos - start:pos - start + size]
            pos += size

        ready = min(self.end, received_at - self.BUFFER)
        self._emit(ready // self.BLOCK * self.BLOCK)

    def finish(self):
        self._emit(self.end)

    def _emit(self, end):
        while self.output_at < end:
            size = min(end - self.output_at, self.BLOCK)
            block = self.blocks.pop(self.output_at, None)
            if block is None:
                data = bytes(size * 2)
            else:
                data = np.clip(block[:size], -32768, 32767).astype("<i2").tobytes()
            self.capture(data, size, None, "")
            self.output_at += size


class MixedAudioStream:
    def __init__(self, open_microphone, capture):
        self.packets = queue.SimpleQueue()
        self.mixer = PcmMixer(capture)
        self.started = ExitStack()
        self.worker = None
        with ExitStack() as opening:
            microphone = open_microphone(self._input("mic"))
            opening.callback(microphone.close)
            system = SystemAudioStream(self._input("system"))
            opening.callback(system.close)
            self.streams = (microphone, system)
            self.resources = opening.pop_all()

    def _input(self, source):
        def receive(data, frames, timing, status):
            self.packets.put((source, time.monotonic_ns(), bytes(data), str(status) if status else ""))

        return receive

    def start(self):
        self.origin = time.monotonic_ns()
        for stream in self.streams:
            stream.start()
            self.started.callback(stream.stop)
        worker = threading.Thread(target=self._mix, name="audio-mixer", daemon=True)
        worker.start()
        self.worker = worker

    def _mix(self):
        for source, stamp, data, status in iter(self.packets.get, None):
            received_at = round((stamp - self.origin) * SAMPLE_RATE / 1_000_000_000)
            self.mixer.add(source, received_at, data, status)
        self.mixer.finish()

    def stop(self):
        try:
            self.started.close()
        finally:
            if self.worker is not None:
                self.packets.put(None)
                self.worker.join()
                self.worker = None

    def close(self):
        try:
            self.stop()
        finally:
            self.resources.close()
