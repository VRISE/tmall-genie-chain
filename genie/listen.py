#!/usr/bin/env python3
"""Mac 侧的耳朵：麦克风 → 能量断句 → whisper.cpp 转文字 → 日志 + HTTP。

  python3 genie/listen.py                 # 常驻，转写写进 /tmp/genie-listen.log
  python3 genie/listen.py --once 9        # 录 9 秒，打完字就退
  curl localhost:8091                     # 看最近的转写
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import queue
import statistics
import subprocess
import sys
import threading
import time
import urllib.request
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOME = os.path.expanduser("~")
LOG = os.environ.get("LISTEN_LOG", os.path.join(HOME, ".genie", "听到.txt"))
WAVDIR = os.environ.get("LISTEN_WAV", os.path.join(HOME, ".genie", "原始音频"))
BIN = os.environ.get("WHISPER_BIN", os.path.join(HOME, "opt", "whisper", "bin", "whisper-cli"))
MODEL = os.environ.get("WHISPER_MODEL", os.path.join(HOME, "opt", "whisper", "models", "ggml-small-q5_1.bin"))
RATE = 16000
BLOCK = 800          # 50ms
PRE = 8              # 触发前留 0.4s，别吃字头
TAIL = 24            # 静音 1.2s 算一句说完
MIN_LEN = 0.3
GATE_MULT = 4.0
# 本地唤醒词：故意不跟「天猫精灵/花生同学/松鼠同学」撞，两条链各管各的
WAKE = [w for w in os.environ.get("LISTEN_WAKE", "小松鼠").split(",") if w]
BOX_LOCAL = os.environ.get("LISTEN_BOX", "http://192.168.5.42:8092/local")


def log(line: str) -> None:
    stamp = time.strftime("%H:%M:%S")
    with open(LOG, "a") as f:
        f.write(f"{stamp} {line}\n")
    print(stamp, line, flush=True)


def transcribe(path: str) -> str:
    base = path[:-4]
    # stderr 里偶有非 UTF-8 字节，text=True 解码会抛 UnicodeDecodeError 把转写线程整个打死
    subprocess.run([BIN, "-m", MODEL, "-f", path, "-l", "zh", "-t", "8",
                    "--prompt", "以下是普通话的句子。",
                    "-sns", "false", "-ml", "0", "-otxt", "-of", base],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        with open(base + ".txt") as f:
            return " ".join(x.strip() for x in f if x.strip())
    except OSError:
        return ""


def route(text: str) -> None:
    """本地唤醒词 → 整句原话送给盒子派活。天猫精灵只认枚举，我们自己的耳朵没有这个限制。"""
    for w in WAKE:
        i = text.find(w)
        if i < 0:
            continue
        cmd = text[i + len(w):].strip(" ，,。.!！?？、")
        if not cmd:
            continue
        log(f"本地唤醒 → 送给盒子「{cmd}」")
        # 盒子上这一腿可能要点时间（找片子/问豆包），别堵住后面句子的转写
        threading.Thread(target=_post, args=(cmd,), daemon=True).start()
        return


def _post(cmd: str) -> None:
    """盒子回一句该念的话，就由 Mac 的喇叭念出来（本地这条链没有平台替我们发声）。"""
    # urllib 默认会读 macOS 系统代理，走 Clash 到 192.168.5.x 会撞 502 —— 局域网必须直连
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(
                urllib.request.Request(
                    BOX_LOCAL, data=json.dumps({"说": cmd}).encode(),
                    headers={"Content-Type": "application/json"}), timeout=20) as r:
            out = json.loads(r.read().decode("utf-8", "replace"))
        say = (out.get("念") or "").strip()
        if say:
            log(f"盒子回「{say}」（{out.get('干')}）")
            subprocess.run(["say", "-v", "Tingting", "-r", "160", say], check=False)
    except Exception as e:  # noqa: BLE001 - 盒子不在也不能把转写线程带下去
        log(f"送盒子失败：{type(e).__name__}: {e}")


def worker(q: "queue.Queue") -> None:
    while True:
        path, seq = q.get()
        try:
            text = transcribe(path)
            log(f"听到「{text}」" if text else f"(一段杂音，没认出字 {os.path.basename(path)})")
            if text:
                route(text)
        except Exception as e:  # noqa: BLE001 - 一段音频出错不能把耳朵弄聋
            log(f"转写炸了：{type(e).__name__}: {e}")
        finally:
            for ext in (".txt", ".json", ".vtt"):
                try:
                    os.remove(path[:-4] + ext)
                except OSError:
                    pass
            q.task_done()


def dump(frames, seq) -> str | None:
    import numpy as np
    a = np.concatenate([f.reshape(-1) for f in frames]).astype(np.int16)
    if len(a) < RATE * MIN_LEN:
        return None
    os.makedirs(WAVDIR, exist_ok=True)
    path = os.path.join(WAVDIR, f"{int(time.time())}-{seq}.wav")
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(a.tobytes())
    keep = sorted(
        (os.path.join(WAVDIR, f) for f in os.listdir(WAVDIR) if f.endswith(".wav")),
        key=os.path.getmtime, reverse=True)[100:]
    for old in keep:
        try:
            os.remove(old)
        except OSError:
            pass
    return path


def record(seconds: float = 0, q: "queue.Queue" = None) -> None:
    """seconds<=0 = 一直录。每句丢进 q 由后台线程转写。"""
    import numpy as np
    import sounddevice as sd

    owned = q is None
    if owned:
        q = queue.Queue()
        threading.Thread(target=worker, args=(q,), daemon=True).start()

    noise: collections.deque = collections.deque(maxlen=200)
    pre: collections.deque = collections.deque(maxlen=PRE)
    seg: list = []
    state = {"silent": 0, "on": False, "seq": 0}

    def cb(ind, n, frames, status):
        x = ind.reshape(-1).astype(np.float32)
        rms = float(np.sqrt(np.mean(x * x)))
        base = max(statistics.median(noise) if len(noise) > 40 else 0.0, 30.0)
        if rms > base * GATE_MULT:
            if not state["on"]:
                state["on"] = True
                seg.extend(pre)
            seg.append(ind.copy())
            state["silent"] = 0
        elif state["on"]:
            seg.append(ind.copy())
            state["silent"] += 1
            if state["silent"] >= TAIL:
                state["on"] = False
                path = dump(seg[:], state["seq"])
                state["seq"] += 1
                seg.clear()
                if path:
                    q.put((path, path))
        if not state["on"]:
            noise.append(rms)
            pre.append(ind.copy())

    t_end = time.time() + seconds if seconds > 0 else None
    with sd.InputStream(samplerate=RATE, channels=1, dtype="int16",
                        blocksize=BLOCK, callback=cb):
        while t_end is None or time.time() < t_end:
            time.sleep(0.2)
    if state["on"] and seg:
        path = dump(seg[:], state["seq"])
        if path:
            q.put((path, path))
    q.join()


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/health"):
            body = '{"ok": true}'
        else:
            try:
                with open(LOG) as f:
                    body = "".join(f.readlines()[-200:])
            except OSError:
                body = ""
        raw = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *a):
        pass


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", type=float, default=0)
    ap.add_argument("--port", type=int, default=int(os.environ.get("LISTEN_PORT", 8091)))
    ap.add_argument("--no-server", action="store_true")
    a = ap.parse_args()

    for p, what in ((BIN, "whisper-cli"), (MODEL, "模型")):
        if not os.path.exists(p):
            print(f"找不到{what}：{p}", file=sys.stderr)
            return 1
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    os.makedirs(WAVDIR, exist_ok=True)
    if not a.no_server:
        srv = ThreadingHTTPServer(("0.0.0.0", a.port), Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        log(f"耳朵开着 :{a.port}/ → {LOG}")
    if a.once:
        record(a.once)
        return 0
    while True:
        try:
            record(0)
        except KeyboardInterrupt:
            return 0
        except Exception as e:
            log(f"录音断了：{e}")
            time.sleep(2)


if __name__ == "__main__":
    raise SystemExit(main())
