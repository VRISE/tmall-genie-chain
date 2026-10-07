#!/usr/bin/env python3
"""视频腿探测：只看豆包给不给得出能解开的抖音链接，不放电视、不出声。"""
import sys, os, json
sys.path.insert(0, "/opt/genie42")
from genielib import targets  # noqa: E402

for q in sys.argv[1:] or ["动画片"]:
    ask = f"推荐一个{q}的抖音视频，只给链接"
    txt = targets.llm(ask)
    cs = targets.cards()
    share = targets.find_share(txt + " " + " ".join(str(c.get("url") or "") for c in cs))
    print(f"== {q}")
    print("  豆包原话:", txt[:300].replace("\n", "⏎"))
    print("  卡片:", json.dumps(cs, ensure_ascii=False)[:300])
    print("  解出的链接:", share or "（没有）")
