"""Ordered speech translation routes for interactive microphone sessions."""
from __future__ import annotations

import json
import queue
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import httpx
import websocket

from interpreter import FRAME_SAMPLES, SAMPLE_RATE, Segmenter, tencent_url, wav_bytes

PROVIDERS = ("DeepSeek", "Kimi 订阅", "腾讯云")
KEYS = {"DeepSeek": ("deepseek_key",), "Kimi 订阅": ("kimi_key",),
        "腾讯云": ("tencent_appid", "tencent_id", "tencent_key")}
PROMPT = ("Translate the following speech into the opposite language: Chinese to English or "
          "English to Simplified Chinese. Return only the translation, preserving names and meaning.")
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
                       timeout=25, check=True)
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


def translate_text(client, provider, key, source):
    kimi = provider == "Kimi 订阅"
    url = ("https://api.kimi.com/coding/v1/chat/completions" if kimi else
           "https://api.deepseek.com/chat/completions")
    body = {"model": "kimi-for-coding" if kimi else "deepseek-flash",
            "messages": [{"role": "system", "content": PROMPT}, {"role": "user", "content": source}]}
    if not kimi:
        body["thinking"] = {"type": "disabled"}
    reply = client.post(url, headers={"Authorization": f"Bearer {key}",
                                     "User-Agent": "ZhEnLiveInterpreter/1.0"}, json=body)
    reply.raise_for_status()
    text = reply.json()["choices"][0]["message"]["content"].strip()
    if not text:
        raise ValueError("空译文")
    return text


def translate_segment(client, pcm, routes, keys, notify):
    source = ""
    failures = []
    for name in routes:
        try:
            if name == "腾讯云":
                original, target = tencent_segment(pcm, keys)
                source = original or source
                if source and not target:
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
                    return "", ""
                notify(("source", source))
                target = translate_text(client, name, keys[KEYS[name][0]], source)
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


def priority_loop(app):
    """Segment on the capture clock and drain queued speech before ending the session."""
    sid = app.session_id
    keys, routes = app.route_keys, app.active_routes
    jobs = queue.Queue(maxsize=12)
    def process():
        with httpx.Client(timeout=httpx.Timeout(30., connect=8.)) as client:
            while True:
                job = jobs.get()
                if job is None:
                    break
                pcm, start, end = job
                source, target = translate_segment(client, pcm, routes, keys, app.events.put)
                if source or target:
                    app.events.put(("final_pair", start, end, source, target, sid))
    worker = threading.Thread(target=process, daemon=True)
    worker.start()
    segmenter = Segmenter()
    end = app.capture_started
    def enqueue(pcm, *, drain=False):
        if pcm:
            job = (pcm, end - len(pcm) / (SAMPLE_RATE * 2), end)
            try:
                jobs.put(job, block=drain)
            except queue.Full:
                app.events.put(("status", "翻译排队已满，已跳过一段"))
    try:
        while not app.stop_event.is_set() or not app.audio.empty():
            try:
                frame = app.audio.get(timeout=.1)
            except queue.Empty:
                continue
            end += len(frame) / (SAMPLE_RATE * 2)
            enqueue(segmenter.feed(frame))
        enqueue(segmenter.flush(), drain=True)
    finally:
        jobs.put(None)
        worker.join()
