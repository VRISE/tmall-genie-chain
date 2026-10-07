#!/usr/bin/env python3
"""灌语料：把「初一数学」这类话能怎么说全展开，顺便给出台词表里到底能预设多少句。

  python3 console/gen_corpus.py            # 只算条数、写文件
  python3 console/gen_corpus.py --check    # 只查有没有跟已有句子撞车
"""
from __future__ import annotations

import csv
import itertools
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
P_GRADE, P_SUBJECT, P_TOPIC = "grade", "subject", "topic"

# 「看/放」的动词架子：{o} 是槽位，{t} 是尾巴
OPENERS = ["给我看一下", "我要看", "给我看", "帮我播放", "放一个", "放一段", "来一段",
            "找一下", "有没有", "我想看", "给我播", "来一个"]
MID_FIX = ["", "的", "类的", "相关的"]
TAILS = ["视频", "影片", "片子", "视频看看", "的视频", ""]

def template_lines():
    out = []
    for opener in OPENERS:
        for g, s in itertools.product(_read("实体_年级.csv"), _read("实体_学科.csv")):
            for mid, tail in itertools.product(MID_FIX, TAILS):
                core = f"{g}{s}" if not mid else f"{g}{mid}{s}"
                line = f"{opener}{core}的{tail}" if tail else f"{opener}{core}"
                out.append((line, {P_GRADE: g, P_SUBJECT: s}))
    return out


def _read(name):
    p = os.path.join(HERE, name)
    if not os.path.exists(p):
        return []
    with open(p, encoding="utf-8") as f:
        return [l.strip() for l in f if l.strip() and not l.startswith("值")]


def topic_lines():
    out = []
    for opener in OPENERS:
        for t in _read("实体_话题.csv"):
            out.append((f"{opener}{t}的视频", {P_TOPIC: t}))
            out.append((f"{opener}{t}", {P_TOPIC: t}))
    return out


def fixed_lines():
    """不带槽位的：控制、生活、追问。例句式，直接命中。"""
    return [(l, {}) for l in [
        "停止播放", "别放了", "暂停", "继续播放", "下一个", "换一个", "再来一个",
        "上一个", "重播", "大声一点", "小声一点", "关掉吧",
        "今天吃什么菜", "推荐一个炒菜", "教我做一道家常菜",
        "讲个笑话", "今天天气怎么样", "现在几点了",
    ]]


def write_all(path):
    rows = fixed_lines() + topic_lines() + template_lines()
    seen, out = set(), []
    for line, slots in rows:
        if line in seen:
            continue
        seen.add(line)
        out.append((line, slots))
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["语料内容", P_GRADE, P_SUBJECT, P_TOPIC])
        for line, slots in out:
            w.writerow([line, slots.get(P_GRADE, ""), slots.get(P_SUBJECT, ""), slots.get(P_TOPIC, "")])
    return len(out)


def dup_report(path):
    with open(path, encoding="utf-8-sig") as f:
        lines = [r[0] for r in csv.reader(f)][1:]
    c = {}
    for l in lines:
        c[l] = c.get(l, 0) + 1
    bad = {k: v for k, v in c.items() if v > 1}
    over = [l for l in lines if len(re.findall(r"@\{", l)) > 1 and len(l) > 24]
    print(f"共 {len(lines)} 句，重复 {len(bad)} 句，超长多槽 {len(over)} 句")
    for k in list(bad)[:5]:
        print("  重了：", k)


if __name__ == "__main__":
    dst = os.path.join(HERE, "语料.自动.csv")
    n = write_all(dst)
    print(f"写了 {n} 句 → {dst}")
    if "--check" in sys.argv:
        dup_report(dst)
