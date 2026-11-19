"""Ordered speech translation routes for interactive microphone sessions."""
from __future__ import annotations

import json
import queue
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx
import numpy as np
import websocket

from interpreter import FRAME_SAMPLES, SAMPLE_RATE, tencent_url, wav_bytes

PROVIDERS = ("DeepSeek", "Kimi 订阅", "腾讯云")
KEYS = {"DeepSeek": ("deepseek_key",), "Kimi 订阅": ("kimi_key",),
        "腾讯云": ("tencent_appid", "tencent_id", "tencent_key")}
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


def tencent_segment(pcm, keys):
    url = tencent_url(*(keys[k] for k in KEYS["腾讯云"]))
    ws = websocket.create_connection(url, timeout=8)
    try:
        step = FRAME_SAMPLES * 2
        for offset in range(0, len(pcm), step):
            ws.send_binary(pcm[offset:offset + step])
            if offset + step < len(pcm):
                time.sleep(FRAME_SAMPLES / SAMPLE_RATE)
        ws.send(json.dumps({"type": "end"}))
        results = {}
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            data = json.loads(ws.recv())
            if data.get("code", 0):
                raise RuntimeError(f"服务错误 {data['code']}")
            item = data.get("result")
            if item:
                results[item.get("start_time", 0)] = (item.get("source_text", ""), item.get("target_text", ""))
            if data.get("final") == 1:
                pairs = [results[k] for k in sorted(results)]
                return tuple(" ".join(p[i] for p in pairs).strip() for i in (0, 1))
        raise TimeoutError()
    finally:
        ws.close()


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
            if name == "腾讯云":
                original, target = tencent_segment(pcm, keys)
                source = original or source
                if not original or not target:
                    raise ValueError("空译文")
            else:
                if not source and name == "Kimi 订阅":
                    source = local_transcript(pcm)
                elif not source:
                    r = client.post("https://api.deepseek.com/v1/tools/asr",
                                    headers={"Authorization": "Bearer " + keys["deepseek_key"]},
                                    files={"file": ("speech.wav", wav_bytes(pcm), "audio/wav")})
                    r.raise_for_status()
                    source = r.json()["text"].strip()
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


def minute_boundary(buffer):
    """At the minute mark, prefer the end of a quiet 200 ms interval near the boundary."""
    window = SAMPLE_RATE // 5
    lookback = 3 * SAMPLE_RATE
    samples = np.frombuffer(buffer, dtype="<i2", count=lookback, offset=MINUTE_BYTES - 2 * lookback)
    blocks = samples.astype(np.float32).reshape(-1, window)
    rms = np.sqrt(np.mean(blocks * blocks, axis=1))
    threshold = min(120., max(8., np.max(rms) * .12))
    quiet = np.flatnonzero(rms <= threshold)
    if quiet.size:
        return MINUTE_BYTES - (len(rms) - 1 - int(quiet[-1])) * window * 2
    return MINUTE_BYTES


def minute_batches(app):
    # The microphone callback keeps queuing frames while the consumer is contacting a provider.
    buffer = bytearray()
    total_bytes = 0
    while not app.stop_event.is_set() or not app.audio.empty():
        try:
            buffer.extend(app.audio.get(timeout=.1))
        except queue.Empty:
            continue
        while len(buffer) >= MINUTE_BYTES:
            cut = minute_boundary(buffer)
            pcm = bytes(buffer[:cut])
            del buffer[:cut]
            yield pcm, total_bytes
            total_bytes += cut
    if buffer:
        yield bytes(buffer), total_bytes


def priority_loop(app):
    sid = app.session_id
    previous = app.history_store.last_translation(sid)
    with httpx.Client(timeout=httpx.Timeout(90., connect=8.)) as client:
        for pcm, offset in minute_batches(app):
            if not any(pcm):
                continue
            start = app.capture_started + offset / (2 * SAMPLE_RATE)
            end = app.capture_started + (offset + len(pcm)) / (2 * SAMPLE_RATE)
            app.events.put(("status", "正在识别和翻译本段…"))
            source, target = translate_segment(client, pcm, app.active_routes, app.route_keys,
                                               app.events.put, previous)
            if source or target:
                app.events.put(("final_pair", start, end, source, target, sid))
            if source and target:
                previous = (source, target)
