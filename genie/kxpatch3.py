#!/usr/bin/env python3
"""把 2026-09-30 下午那轮「文案/路由」修复做成幂等站点，防止隔壁会话把基线推回来时丢掉。

用法：
    python3 kxpatch3.py <代码根目录>          # dry-run，只看锚点对不对
    python3 kxpatch3.py <代码根目录> --write   # 落盘
锚点对不上只会打印出来，绝不覆盖别人的改动。
"""
import os
import sys

ROOT = sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1].startswith("-") else "."

SITES = [
    # ── plan.py：出题/造句/讲故事别去放片 ──────────────────────────────
    # 平台把整句话塞进「问题」槽，_search 只要开头是「帮我/给我」就返片名，
    # 于是「帮我出几道书法题」当场答应「这就放给你」。
    ("genielib/plan.py",
     '             "讲故事", "听故事"]',
     '             "讲故事", "听故事",\n'
     '             # 「帮我出几道书法题」是被念出来的动词壳骗进播放腿的：出题/造句是要模型答题，不是放片\n'
     '             "出题", "出一道", "出几道", "来一道", "的一道", "造句", "应用题", "判断题",\n'
     '             "选择题", "填空题", "练习题", "小实验", "造个句", "造句子"]',
     "出题族进 ASK_WORDS"),

    # 没量词也算：「给我出一年级的书法题」动词壳一剥就露出「出…题」。
    # 注意别写成裸的 startswith(("出","考"))——「我要听出山」「考拉的视频」会被误判成提问。
    ("genielib/plan.py",
     '    body = re.sub(r"^一[下段首篇个支部门]", "", body)',
     '    # 「给我出一年级的书法题」「给我讲杯弓蛇影」：动词壳一剥就露出出题/讲故事的字头，\n'
     '    # 这是要模型答题不是要放片。别写宽了——「播放出山」那首歌也叫「出山」。\n'
     '    if re.match(r"^出.{0,8}?[道题份套]", body) or body.startswith("讲"):\n'
     '        return ""\n'
     '    body = re.sub(r"^一[下段首篇个支部门]", "", body)',
     "出题/讲故事字头规则"),

    # 平台会把槽位念短：说「怎么做手工贺卡」实体只对上前半句，回传「怎么做手工」。
    ("genielib/plan.py",
     '        p.action = "ask"\n        p.query = sent\n'
     '        p.title = p.query[:20]',
     '        p.action = "ask"\n        p.query = sent\n'
     '        # 实体只对上前半句时槽位会被截短（说「怎么做手工贺卡」回的是「怎么做手工」），\n'
     '        # 原话更长又包着这句，就按原话问，别答错题\n'
     '        full = _strip_skill(utt)\n'
     '        if full and full != sent and sent in full:\n'
     '            p.query = full\n'
     '        p.title = p.query[:20]',
     "槽位截短救援"),

    # ── targets.py：念出来的味道 ─────────────────────────────────────
    ("genielib/targets.py",
     'ASKBACK = _ASKBACK = re.compile("要不要|需不需要|还用不用|想不想|还想|我可以帮|还有什么"\n'
     '                      "|你来|你想让|让我原样|复述|出几道|考考你")',
     'ASKBACK = _ASKBACK = re.compile("要不要|需不需要|还用不用|想不想|还想|我可以帮|还有什么"\n'
     '                      "|你来|你想让|让我原样|复述|出几道|考考你|下一题|继续讲|还想听|要不要接着")',
     "追问尾巴词表加宽"),

    ("genielib/targets.py",
     '    s = re.sub(r"([。！？；，])\\1+", r"\\1", s)  # 两行都带句号时会叠成「。。」，TTS 听着卡顿',
     '    s = re.sub(r"([。！？；，])\\1+", r"\\1", s)  # 两行都带句号时会叠成「。。」，TTS 听着卡顿\n'
     '    # 「什么？。」是上下两行粘出来的：逗号让位给句号，问号叹号后面不叠第二个\n'
     '    s = re.sub(r"[，、](?=[。！？；])", "", s)\n'
     '    s = re.sub(r"([。！？；])[。！？；，]+", r"\\1", s)\n'
     '    # 音箱念「斜杠」「破折号」「摄氏度符号」都会卡，换成能读的字\n'
     '    s = s.replace("℃", "摄氏度")\n'
     '    s = re.sub(r"(?<=\\d)[/／](?=\\d)", "到", s)\n'
     '    s = re.sub(r"[–—~]", "到", s)\n'
     '    s = s.replace("/", "、").replace("／", "、")',
     "叠标点/符号换成能念的字"),

    # 10-01 补的第 7 站：% 整个挪到数字前面才念对，×÷ 换字。
    # `+` 和数字中间的 `-` 故意不碰：「常规+加时」「4-1击败」该念「加」「比」，代码分不出。
    ("genielib/targets.py",
     '    s = s.replace("/", "、").replace("／", "、")',
     '    s = s.replace("/", "、").replace("／", "、")\n'
     '    # % 得整个挪到数字前面才念得对（99%→百分之99）；×÷ 换字。\n'
     '    # 但 `+` 和数字中间的 `-` 故意不动：「常规+加时」「AI小兵+其他玩家」「4-1击败」\n'
     '    # 念成「加」「和」「比」才对，代码分不出来，这种得回库里重问。\n'
     '    s = re.sub(r"(\\d+(?:\\.\\d+)?)\\s*[%％]", r"百分之\\1", s)\n'
     '    s = s.replace("×", "乘").replace("÷", "除")',
     "百分号和乘除号"),

    ("genielib/targets.py",
     '    if out.count("《") > out.count("》"):  # 别断在书名号中间\n'
     '        out = out[:out.rfind("《")]',
     '    if out.count("《") > out.count("》"):  # 别断在书名号中间\n'
     '        out = out[:out.rfind("《")]\n'
     '    while out.count("（") > out.count("）"):  # 括号截一半，音箱念个「左括号」就没了下文\n'
     '        out = out[:out.rfind("（")]',
     "括号不成对就回退"),

    # 10-01 第 8 站：库里存过「今天星期三」「明天（9月30日）中雨」，隔天就是胡说。
    # 问今天/现在的当场算，天气新闻每次现问。
    ("genielib/targets.py",
     'def offline_answer(question: str) -> str:\n'
     '    if not OFFLINE_DIR:',
     '# 答案库是存死的，天气/新闻存一次就错一天，日历存一次就错一年\n'
     '_STALE_Q = re.compile(r"天气|气温|多少度|新闻|最新")\n'
     '\n'
     '\n'
     'def _clock_answer(question: str) -> str:\n'
     '    """问今天/问现在的，当场算，别查库。盒子只有 UTC，家里说的是北京时间，自己 +8。\n'
     '\n'
     '    只认带「今天/现在/此刻/今日」的：「重阳节是几月几号」「电影几点开始」答成今天就是胡说。\n'
     '    """\n'
     '    if not re.search(r"今天|今晚|此刻|现在|今日", question):\n'
     '        return ""\n'
     '    if re.search(r"明天|后天|大后天|下周|上周|明年|去年", question):\n'
     '        return ""\n'
     '    t = time.gmtime(time.time() + 8 * 3600)\n'
     '    day = "一二三四五六日"[t.tm_wday]\n'
     '    head = "礼拜" if "礼拜" in question else "星期"\n'
     '    if "几点" in question:\n'
     '        return f"现在{t.tm_hour}点{t.tm_min}分。"\n'
     '    if "几号" in question or "日期" in question:\n'
     '        return f"今天是{t.tm_mon}月{t.tm_mday}号，{head}{day}。"\n'
     '    if "星期" in question or "礼拜" in question:\n'
     '        return f"今天{head}{day}。"\n'
     '    return ""\n'
     '\n'
     '\n'
     'def offline_answer(question: str) -> str:\n'
     '    now = _clock_answer(question)\n'
     '    if now:\n'
     '        return now\n'
     '    if _STALE_Q.search(question):\n'
     '        # 天气/新闻这类宁可每次现问，也别把去年存的念给孩子听\n'
     '        return ""\n'
     '    if not OFFLINE_DIR:',
     "日历/天气不查库"),

    ("genielib/targets.py",
     'def remember(question: str, answer: str) -> str:\n'
     '    """存进答案库，返回真正会念出来的那句（没存成就是空串）。"""\n'
     '    if not OFFLINE_DIR or not answer:\n'
     '        return ""',
     'def remember(question: str, answer: str) -> str:\n'
     '    """存进答案库，返回真正会念出来的那句（没存成就是空串）。"""\n'
     '    if not OFFLINE_DIR or not answer:\n'
     '        return ""\n'
     '    if _clock_answer(question) or _STALE_Q.search(question):\n'
     '        return ""        # 当场算的、隔天就旧的，存下来就是留一条过期答案',
     "日历/天气不入库"),
]

cache = {}
hit = 0
for rel, old, new, mark in SITES:
    path = os.path.join(ROOT, rel)
    src = cache.setdefault(path, open(path, encoding="utf-8").read())
    if new in src:
        print(f"跳过（已生效）：{mark}")
        continue
    if src.count(old) != 1:
        print(f"！锚点对不上（{src.count(old)} 处）：{rel} · {mark}")
        continue
    cache[path] = src.replace(old, new, 1)
    hit += 1
    print(f"打上：{rel} · {mark}")

if "--write" in sys.argv:
    for path, src in cache.items():
        open(path, "w", encoding="utf-8").write(src)
        print("已写：", path)
else:
    print(f"(dry-run：{hit} 处待打，加 --write 才落盘)")
