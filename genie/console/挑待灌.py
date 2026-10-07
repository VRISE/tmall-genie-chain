#!/usr/bin/env python3
"""挑这一批要灌库的句子。

规则照抄 genied 的命中逻辑：存了 k 之后，y 能秒答的条件是
  norm(y)==k 或 core(y)==core(k) 或 (len(k)>=5 且 k 与 y 互为子串)。
所以一条答案实际能顶几句是可算的 —— 按边际增益贪心，别把豆包额度
浪费在同一主题的第二句上。
"""
import json
import re

LEAD = re.compile(r"^(能不能|能|可以|帮我|给我|请|我想|我要)?"
                  r"(来一段|讲讲看|讲讲|说说|说一说|介绍一下|介绍|了解|听听|听|看看|看|放|讲|说)+")
TAIL = re.compile(r"(的|了)?(小故事|故事|历史|知识|来历|由来|简介|是谁|是什么"
                  r"|怎么来的|能放|能看|可以看|可以放|想看|要看|吗|吧|呀|啊|呢)+$")
PUNCT = re.compile(r"[？?！。，,、\s]")


def norm(s):
    return PUNCT.sub("", s)


def core(q):
    s = TAIL.sub("", LEAD.sub("", norm(q)))
    return s or norm(q)


def covers(k, ys):
    """存了 k 之后，ys 里哪些能秒答。"""
    n, c = norm(k), core(k)
    return {y for y in ys if norm(y) == n or core(y) == c
            or (len(n) >= 5 and (n in norm(y) or norm(y) in n))}


lib = json.load(open("/tmp/ans42-dump.json"))
allq = json.load(open("/tmp/qvals.json"))
todo = [x for x in allq if not any(covers(k, [x]) for k in lib)]
remaining = set(todo)
print(f"实体 {len(allq)} 句，已有答案 {len(allq) - len(todo)}，待灌 {len(todo)}")

picks = []
while len(picks) < 20 and remaining:
    best, bestset = None, set()
    for x in list(remaining):
        g = covers(x, remaining)
        if len(g) > len(bestset):
            best, bestset = x, g
    if not best or not bestset:
        break
    picks.append(best)
    remaining -= bestset

print(f"贪心选出 {len(picks)} 条，顶掉 {len(todo) - len(remaining)} 句")
# 不够 20 条就按孩子最常开口的句式补
PRIO = ["是谁", "的故事", "吃什么", "为什么", "怎么做", "在哪里", "有什么", "怎么办", "的由来"]
i = 0
while len(picks) < 20 and i < len(PRIO):
    for x in todo:
        if len(picks) >= 20:
            break
        if x not in picks and PRIO[i] in x and all(norm(x) not in norm(p) for p in picks):
            picks.append(x)
    i += 1

for p in picks:
    print("  ", p, "顶", len(covers(p, set(todo))))
json.dump(picks[:20], open("/tmp/batch.json", "w"), ensure_ascii=False)
