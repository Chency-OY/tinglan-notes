"""Ordered speech translation routes for interactive microphone sessions."""
from __future__ import annotations

import queue
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

import httpx
import webrtcvad

from interpreter import SAMPLE_RATE, wav_bytes

PROVIDERS = ("DeepSeek", "Kimi 订阅")
KEYS = {"DeepSeek": ("deepseek_key",), "Kimi 订阅": ("kimi_key",)}
PROMPT = ("Interpret the latest transcript from Chinese into English or English into Simplified Chinese. "
          "Previous turns are context only: maintain the same terminology and resolve references across "
          "segments. Translate only the latest turn. Preserve numbers, names, qualifications and negations. "
          "Do not summarize, repeat earlier material or invent missing words. Return only the translation.")
MINUTE_BYTES = 60 * SAMPLE_RATE * 2
WHISPER_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).parent)) / "whisper"


def priority_order(value):
    order = []
    for provider in [*value.split("|"), *PROVIDERS]:
        if provider in PROVIDERS and provider not in order:
            order.append(provider)
    return order


def local_transcript(pcm):
    exe = WHISPER_DIR / "whisper-cli.exe"
    model = WHISPER_DIR / "ggml-base-q5_1.bin"
    if not exe.is_file() or not model.is_file():
        raise RuntimeError("缺少本地语音模型")
    with tempfile.TemporaryDirectory() as tmp:
        subprocess.run([str(exe.resolve()), "-m", str(model.resolve()), "-f", "-", "-l", "auto",
                        "-otxt", "-of", "segment", "-nt", "-np", "-t", "4"], input=wav_bytes(pcm),
                       cwd=tmp, capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW,
                       timeout=90, check=True)
        return (Path(tmp) / "segment.txt").read_text(encoding="utf-8-sig").strip()


def translate_text(client, provider, key, source, previous=None):
    kimi = provider == "Kimi 订阅"
    url = ("https://api.kimi.com/coding/v1/chat/completions" if kimi else
           "https://api.deepseek.com/chat/completions")
    messages = [{"role": "system", "content": PROMPT}]
    if previous:
        messages.extend([{"role": "user", "content": previous[0][-900:]},
                         {"role": "assistant", "content": previous[1][-900:]}])
    messages.append({"role": "user", "content": source})
    body = {"model": "kimi-for-coding" if kimi else "deepseek-flash", "messages": messages}
    if not kimi:
        body["thinking"] = {"type": "disabled"}
    reply = client.post(url, headers={"Authorization": f"Bearer {key}",
                                     "User-Agent": "ZhEnLiveInterpreter/1.0"}, json=body)
    reply.raise_for_status()
    text = reply.json()["choices"][0]["message"]["content"].strip()
    if not text:
        raise ValueError("空译文")
    return text


def translate_segment(client, pcm, routes, keys, notify, previous=None):
    source = ""
    failures = []
    for name in routes:
        try:
            if not source and name == "Kimi 订阅":
                source = local_transcript(pcm)
            elif not source:
                response = client.post("https://api.deepseek.com/v1/tools/asr",
                                       headers={"Authorization": "Bearer " + keys["deepseek_key"]},
                                       files={"file": ("speech.wav", wav_bytes(pcm), "audio/wav")})
                response.raise_for_status()
                source = response.json()["text"].strip()
            if source.strip().upper() == "[BLANK_AUDIO]":
                return "", ""
            if not source:
                raise ValueError("未识别到文字")
            notify(("source", source))
            target = translate_text(client, name, keys[KEYS[name][0]], source, previous)
            if failures:
                notify(("status", f"{'，'.join(failures)} · 本段已切换至 {name}"))
            else:
                notify(("status", f"正在收音 · {name}"))
            return source, target
        except Exception as exc:
            reason = (f"HTTP {exc.response.status_code}" if isinstance(exc, httpx.HTTPStatusError)
                      else str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__)
            failures.append(f"{name} {reason}")
    notify(("status", "本段翻译失败 · " + "，".join(failures)))
    return source, ""


VAD_FRAME_BYTES = SAMPLE_RATE * 2 // 50  # WebRTC accepts 20 ms frames.
PAUSE_BYTES = SAMPLE_RATE * 2


def vad_frames(app):
    remainder = b""
    while not app.stop_event.is_set() or not app.audio.empty():
        try:
            data = remainder + app.audio.get(timeout=.1)
        except queue.Empty:
            continue
        complete = len(data) - len(data) % VAD_FRAME_BYTES
        for pos in range(0, complete, VAD_FRAME_BYTES):
            yield data[pos:pos + VAD_FRAME_BYTES]
        remainder = data[complete:]
    if remainder:
        yield remainder


def speech_batches(app):
    detector = webrtcvad.Vad(1)
    heard_voice = False
    audio = bytearray()
    offset = quiet_bytes = 0
    for frame in vad_frames(app):
        voiced = detector.is_speech(frame.ljust(VAD_FRAME_BYTES, b"\0"), SAMPLE_RATE) and any(frame)
        offset += len(frame)
        if not audio and not any(frame):
            continue  # Omit only digital silence between segments. Retain quiet or uncertain speech.
        heard_voice = heard_voice or voiced
        audio.extend(frame)
        quiet_bytes = 0 if voiced else quiet_bytes + len(frame)
        if (heard_voice and quiet_bytes >= PAUSE_BYTES) or len(audio) >= MINUTE_BYTES:
            yield bytes(audio), offset - len(audio)
            audio.clear()
            heard_voice = False
            quiet_bytes = 0
    if audio:
        yield bytes(audio), offset - len(audio)


def priority_loop(app):
    sid = app.session_id
    jobs = queue.SimpleQueue()

    def segment_audio():
        try:
            for batch in speech_batches(app):
                jobs.put(batch)
        except Exception as exc:
            app.events.put(("status", f"音频分段失败 · {type(exc).__name__}"))
        finally:
            jobs.put(None)

    # ASR, HTTP and provider retries never run on the capture or segmentation threads.
    producer = threading.Thread(target=segment_audio, daemon=True)
    producer.start()
    try:
        if app.previous_saved is not None:
            app.previous_saved.wait()
        previous = app.history_store.last_translation(sid) if app.translation_enabled else None
        with httpx.Client(timeout=httpx.Timeout(90., connect=8.)) as client:
            for pcm, offset in iter(jobs.get, None):
                start = app.capture_started + offset / (2 * SAMPLE_RATE)
                end = app.capture_started + (offset + len(pcm)) / (2 * SAMPLE_RATE)
                if app.translation_enabled:
                    app.events.put(("status", "正在识别和翻译本段…"))
                    source, target = translate_segment(client, pcm, app.active_routes, app.route_keys,
                                                       app.events.put, previous)
                else:
                    app.events.put(("status", "正在本地转写本段…"))
                    target = ""
                    try:
                        source = local_transcript(pcm)
                    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
                        reason = str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__
                        app.events.put(("status", f"本段转写失败 · {reason}"))
                        continue
                    if source.upper() == "[BLANK_AUDIO]":
                        continue
                    app.events.put(("status", "正在收音 · 本地转写"))
                if source or target:
                    app.events.put(("final_pair", start, end, source, target, sid))
                if source and target:
                    previous = (source, target)
    finally:
        app.stop_event.set()
        producer.join()
