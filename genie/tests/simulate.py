#!/usr/bin/env python3
"""拿天猫精灵真形状的请求打自己，看回的 JSON 平台能不能吃。

  python3 tests/simulate.py            # 起一个干跑的实例，全走一遍
  BASE=http://192.168.5.142:8090 python3 tests/simulate.py   # 打线上那个
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
BASE = os.environ.get("BASE", "http://127.0.0.1:8099")

CASES = [
    # 方案A：平台解析完，intent + slot 都给全
    ("/aligenie/corpus", {
        "sessionId": "s1", "utterance": "给我看一下初一的数学视频", "intentName": "看学习视频",
        "slotEntities": [
            {"intentParameterName": "grade", "originalValue": "初一", "standardValue": "初一"},
            {"intentParameterName": "subject", "originalValue": "数学", "standardValue": "数学"}]},
        "play"),
    # 方案B：只有原话，什么意图都没命中，走默认意图
    ("/aligenie/nlu", {
        "sessionId": "s2", "utterance": "给我看一下初一的数学视频", "intentName": "default",
        "slotEntities": []}, "play"),
    ("/aligenie/nlu", {
        "sessionId": "s3", "utterance": "两只小狗，宇宙有多大", "intentName": "default",
        "slotEntities": []}, "ask"),
    ("/aligenie/nlu", {
        "sessionId": "s4", "utterance": "别放了", "intentName": "default",
        "slotEntities": []}, "stop"),
    ("/aligenie/nlu", {
        "sessionId": "s5", "utterance": "下一个", "intentName": "default",
        "slotEntities": []}, "next"),
    ("/aligenie/nlu", {
        "sessionId": "s6", "utterance": "播放三角洲的视频", "intentName": "default",
        "slotEntities": []}, "play"),
    # 合流口：按 intentName 自己认路
    ("/aligenie/webhook", {
        "sessionId": "s7", "utterance": "给我看一下跳舞的视频", "intentName": "看视频",
        "slotEntities": [{"intentParameterName": "topic", "originalValue": "跳舞", "standardValue": "跳舞"}]},
        "play"),
    # 猪八戒：意图命中但「问题」取不到值，整句还在 utterance 里
    ("/aligenie/webhook", {
        "sessionId": "s8", "utterance": "猪八戒，辣椒炒肉怎么做", "intentName": "zhubajie",
        "slotEntities": []}, "ask"),
    # 猪八戒：追问第二轮，平台把「问题」填进来
    ("/aligenie/webhook", {
        "sessionId": "s9", "utterance": "宇宙有多大", "intentName": "zhubajie",
        "slotEntities": [{"intentParameterName": "问题", "originalValue": "宇宙有多大",
                          "standardValue": "宇宙有多大"}]}, "ask"),
    # 真机 14:07 的形状：裸喊调用词，但平台把上一轮的槽位一起带回来了 → 不能重答
    ("/aligenie/webhook", {
        "sessionId": "s10", "utterance": "猪八戒", "intentName": "zhubajie",
        "slotEntities": [{"intentParameterName": "问题", "originalValue": "辣椒炒肉怎么做",
                          "standardValue": "辣椒炒肉怎么做"}]}, "greet"),
    # 05:44 真机：调用词换成「花生同学」之后这条兜底失效了（词不在 SKILL_WORDS 里）→ 拿旧槽位重答了一遍
    ("/aligenie/webhook", {
        "sessionId": "s11", "utterance": "花生同学", "intentName": "zhubajie",
        "slotEntities": [{"intentParameterName": "问题", "originalValue": "红烧肉怎么做",
                          "standardValue": "红烧肉怎么做"}]}, "greet"),
]


def _opener():
    # 本机挂了代理时，urllib 会把 127.0.0.1 也丢给代理，直接 502
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def get(url, timeout=6):
    with _opener().open(url, timeout=timeout) as r:
        return r.read()


def post(path, obj):
    req = urllib.request.Request(BASE + path,
                                 data=json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with _opener().open(req, timeout=15) as r:
        return r.headers.get("Content-Type"), r.read()


def wait_up(timeout=20):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            get(BASE + "/health", 2)
            return True
        except Exception:  # noqa: BLE001
            time.sleep(0.3)
    return False


def main():
    proc = None
    if os.environ.get("BASE"):
        base = BASE
    else:
        env = dict(os.environ, GENIE_PORT="8099", GENIE_DRY="1", GENIE_LAN_CHECK="0",
                   GENIE_VERIFY=os.path.join(HERE, "verify"), GENIE_DATA="/tmp/genie-test")
        proc = subprocess.Popen([sys.executable, os.path.join(ROOT, "genied.py")], env=env)
        base = "http://127.0.0.1:8099"
        globals()["BASE"] = base
        if not wait_up():
            print("起不来")
            proc.terminate()
            return 2
        time.sleep(0.3)

    bad = 0
    for path, obj, want in CASES:
        t0 = time.time()
        ctype, raw = post(path, obj)
        took = time.time() - t0
        try:
            d = json.loads(raw)
        except Exception:  # noqa: BLE001
            print(f"✗ {obj['utterance'][:16]:18} 回来的不是 json：{raw[:80]}")
            bad += 1
            continue
        v = d.get("returnValue", {})
        ok = ("application/json" in (ctype or "")) and v.get("reply") \
            and v.get("resultType") in ("RESULT", "ASK_INF") \
            and v.get("executeCode") == "SUCCESS"
        got = "greet" if v.get("reply") == "您说。" else \
            ("ask" if want == "ask" else                  # 答案文本是模型给的，只认它回了话
             ("stop" if "停" in v.get("reply", "") else
              ("next" if "换" in v.get("reply", "") else "play")))
        flag = "✓" if ok and got == want else "✗"
        if flag == "✗":
            bad += 1
        print(f"{flag} {obj['utterance'][:16]:18} {took:.2f}s {flag and ''}{v.get('reply')!r}")
    # 校验文件那一跳：平台是同步抓的，必须 200 + 纯文本
    vdir = os.path.join(HERE, "verify")
    os.makedirs(vdir, exist_ok=True)
    with open(os.path.join(vdir, "ab12cd34.txt"), "w") as f:
        f.write("verify-token-123")
    try:
        raw = get(base + "/aligenie/ab12cd34.txt")
        ok = raw == b"verify-token-123"
        print(("✓ " if ok else "✗ ") + f"校验文件 /aligenie/ab12cd34.txt → {raw[:30]!r}")
        bad += 0 if ok else 1
    except Exception as e:  # noqa: BLE001
        # 打远端时文件在板子上，本地这个不算数
        print("⚠ 校验文件没放（等平台的文件名再放）：" + str(e))
    # 状态口会去探那几台机器在不在，机器没开时要等探测超时
    st = json.loads(get(base + "/status", 20))
    print("状态：", json.dumps({k: st[k] for k in ("player", "screen", "llm")}, ensure_ascii=False))
    if proc:
        proc.terminate()
    print("全过" if not bad else f"{bad} 条不对")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
