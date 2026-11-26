"""Windows WASAPI loopback capture from the default playback device."""

import audioop

import numpy as np

from interpreter import SAMPLE_RATE


class SystemAudioStream:
    def __init__(self, capture):
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
