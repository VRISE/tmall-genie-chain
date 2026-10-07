#!/usr/bin/env python3
"""天猫精灵的后台：收平台的 POST，判成干什么活，派给手机/NAS，再回一句能听的话。

  POST /aligenie/corpus   方案A：靠语料+实体命中，平台把 intent 和 slot 都算好了
  POST /aligenie/nlu      方案B：默认意图兜底，只有原话，理解归我们自己
  POST /aligenie/webhook  两条路合到一处，按 intentName 自动分
  GET  /aligenie/xxx.txt  平台配置 webhook 时要抓的校验文件
  GET  /                  最近的句子和干了什么（本机看的页）
"""
from __future__ import annotations

import html
import json
import os
import re
import socketserver
import sys
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from genielib import aligenie, plan, targets  # noqa: E402

PORT = int(os.environ.get("GENIE_PORT", "8090"))
VERIFY_DIR = os.environ.get("GENIE_VERIFY", "/etc/genie/aligenie")
# 实测：1.5s 播、2s 不播（2026-09-29 注入延迟二分出来的）—— 这一跳里绝不放大模型
FAST_BUDGET = float(os.environ.get("GENIE_BUDGET", "1.5"))

# 只有本地自测会关：开着才会去探那几台机器在不在
LAN_CHECK = os.environ.get("GENIE_LAN_CHECK", "1") == "1"

DEFAULT_INTENTS = {"", "default", "Default", "默认意图", "unknow", "unknown", "welcome", "欢迎",
                   "nlunlu"}  # nlunlu=三只松鼠(120266) 那个 0 语料 0 参数的空默认意图

LOG = deque(maxlen=200)
LOCK = threading.Lock()
PENDING = deque(maxlen=50)

# ---- 当场报菜名（resultType SELECT）：候选只读盒子本地文件，1.5 秒里出得来 ----
MENU_FILE = os.path.join(targets.DATA_DIR, "片单.txt")
# 两条链各要各的候选：这份不碰上面那份，免得把隔壁的菜名盖掉
MENU_FILE_BY_INTENT = {199486: "片单-花生.txt"}
# 平台要求 askedInfos 带 intentId；只放行这三个意图，别的技能原样走 ASK_INF
# 199486=猪八戒(120248，花生同学) 199538=豆包同学(120282，现在真机在用的)
MENU_INTENTS = {199486: "问题", 199538: "问题"}
# 只认整句；「有哪些」「有什么」这类裸疑问词不当触发词——它们是正常提问的尾巴，不是点菜
MENU_TRIGGER = ["你能干什么", "你能帮我干什么", "你可以干什么", "你可以帮我干什么",
                "你都会什么", "你会什么", "你能做什么", "报菜名", "菜单", "片单"]
MENU_MAX = 10
SENT_MENU: dict = {}    # sessionId -> 我发出去的那份候选；第二轮下标要照着它查

# ---- 续播上一条：豆包慢，第一次问只回「我查一下啊。」，答案后台算好存库。 ----
# 下次喊「继续回答」这类话，就把上一条（存进 LAST_Q_FILE 的原话）从库里捞出来秒答。
LAST_Q_FILE = os.path.join(targets.DATA_DIR, "上一条.txt")
SENT_Q_FILE = os.path.join(targets.DATA_DIR, "上一条念过.txt")
CONTINUE_TRIGGER = ["继续回答", "继续上一次", "继续上一次回答", "继续上一个回答",
                    "念一下上次的", "上次的答案", "上一条答案", "继续说上一条"]


def _read(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return ""


def _write(path: str, text: str):
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except OSError:
        pass


def last_q() -> str:
    return _read(LAST_Q_FILE)


def remember_q(q: str):
    if not q:
        return
    # 新问题顶掉上一条，「念过」标记也归零
    _write(LAST_Q_FILE, q.strip())
    _write(SENT_Q_FILE, "")


def is_continue_ask(utt: str) -> bool:
    t = re.sub(r"[吗呢啊吧呀哦]+$", "", plan._strip_skill(utt))
    return t in CONTINUE_TRIGGER


def mark_sent(q: str):
    """这条答案已经出过口了（电视播的、音箱续答的都算），别再念第二遍。"""
    if q and q == last_q():
        _write(SENT_Q_FILE, q)


def menu_items(iid=None) -> list:
    f = os.path.join(targets.DATA_DIR, MENU_FILE_BY_INTENT.get(iid) or os.path.basename(MENU_FILE))
    try:
        with open(f, encoding="utf-8") as fh:
            return [x.strip() for x in fh if x.strip()][:MENU_MAX]
    except OSError:
        return []


def menu_reply(intent: str, req: dict, lead: str = ""):
    """回 SELECT：平台念完这句自己开麦，下一句跟这些候选匹配后照常 POST 回来。"""
    iid = req.get("intentId") or MENU_INTENTS.get(intent)
    if iid not in MENU_INTENTS:
        return "", f"menu没成(意图{intent or '-'} id={iid})"
    items = menu_items(iid)
    if not items:
        return "", f"menu没成({os.path.basename(MENU_FILE_BY_INTENT.get(iid, '片单.txt'))}空)"
    sid = req.get("sessionId") or ""
    SENT_MENU[sid] = items
    while len(SENT_MENU) > 50:
        SENT_MENU.pop(next(iter(SENT_MENU)))
    body = aligenie.reply(
        (lead.rstrip("。") + "。" if lead else "") + "有这些：" + "、".join(items) + "，要哪个？",
        result_type="SELECT",
        asked=[{"parameterName": MENU_INTENTS[iid], "intentId": iid}],
        select={"intentParameterName": MENU_INTENTS[iid],
                "candidateList": [{"value": v} for v in items],
                "parameterReplyMatch": True,
                "selectIndexMatch": True})
    return body, f"menu({len(items)})"


def is_menu_ask(utt: str) -> bool:
    """整句对上才算点菜（剥掉调用词和句尾语气词后精确比）。"""
    t = re.sub(r"[吗呢啊吧呀哦]+$", "", plan._strip_skill(utt))
    return t in MENU_TRIGGER


def menu_pick(req: dict) -> str:
    """第二轮平台替我们匹配好候选并给下标。ASR 听歪的字（北及星）也可能对上候选，
    但拿它查答案库就落空，所以一律换回我发出去的那一条。"""
    lst = req.get("selectIndexList") or []
    if not lst:
        return ""
    items = SENT_MENU.get(req.get("sessionId") or "") or menu_items(req.get("intentId"))
    try:
        n = int(lst[0])
    except (TypeError, ValueError):
        return ""
    return items[n] if 0 <= n < len(items) else ""


def log(line: str):
    stamp = time.strftime("%H:%M:%S")
    with LOCK:
        LOG.appendleft(f"{stamp} {line}")
    print(f"{stamp} {line}", flush=True)


def record(entry: dict):
    entry["at"] = time.strftime("%m-%d %H:%M:%S")
    with LOCK:
        PENDING.appendleft(entry)


def dispatch(p: plan.Plan):
    """真去干活。返回 (音箱念的那句, 干了啥)。"""
    if p.action == "stop":
        ok, info = targets.control("stop")
        return p.speak if ok else "手机好像没开。", "stop"
    if p.action == "next":
        ok, _ = targets.control("next")
        return (p.speak if ok else "没找到在放的。"), "next"
    if p.action == "prev":
        ok, _ = targets.control("prev")
        return (p.speak if ok else "没有上一条。"), "prev"
    if p.action in ("volume_up", "volume_down"):
        ok, _ = targets.volume(5 if p.action == "volume_up" else -5)
        return (p.speak if ok else "音量我调不了。"), p.action
    if p.action == "play":
        stuck = targets.llm_blocked()
        if stuck:
            # 找片子靠豆包给链接，它被限流时这条必空手，别当场答应「这就放」
            record({"说": p.speak or p.query, "结果": "没派出去：" + stuck})
            return stuck + "，晚点再放给你。", "llm-blocked"
        if targets.TV_URL and not targets.tv_alive():
            record({"说": p.speak or p.query, "结果": "没派出去：接电视那台不在线"})
            return "电视那台没开，我先记着。", "tv-offline"
        if targets.TV_URL:
            # 找片子要先问豆包一句（5～7 秒），不能拖累这一跳：先应下来，后台放
            threading.Thread(target=tv_slow, args=(p.query or p.title, p.speak), daemon=True).start()
            return p.speak or "放到电视上。", f"tv@{targets.TV_URL}"
        ok, where, info = targets.play_video(p.query, p.title, p.url)
        if not ok:
            record({"说": p.speak or p.query, "结果": "没派出去：" + info[:120]})
            return "手机和盒子都没开，我先记着。", "play-fail"
        record({"说": (p.speak or p.query), "结果": f"→ {where} {info[:80]}"})
        return p.speak, f"play@{os.path.basename(where)}:{p.title}"
    if p.kind == "audio":
        ok, info = targets.play_audio(p.query)
        return (p.speak if ok else "曲库没开。"), "audio"
    return "我还不会这个。", "noop"


def tv_slow(query: str, spoken: str):
    """音箱先应了，片子后台找：豆包给的抖音链接放到这台盒子接的电视上。"""
    ok, info = targets.tv_search_play(query)
    log(f"放「{query}」→ 上电视={ok} {info[:120]}")
    record({"说": query, "结果": ("电视上放了：" + info) if ok else ("电视没放成：" + info)})
    if not ok:
        # 没片源别只说「没找到」：按「没视频就把豆包的声音从电视放出来」这条走
        answer_slow(query)


def teach(qs):
    """只把答案存进库，不出声 —— 批量备料时用，别占着电视。"""
    if targets.LLM_OFF:
        log(f"灌库被阻断：{len(qs)} 条一条没打豆包")
        return
    for q in qs:
        if targets.offline_answer(q):
            continue
        text = targets.llm(q, use_hist=False)
        kept = targets.remember(q, text)
        log(f"灌「{q}」→ {kept or '没入库，豆包原话：' + (text[:60] or '空的，没答上')}")
        time.sleep(0.5)  # 豆包有限流，一条一条来
    log(f"灌库这一批跑完，{len(qs)} 条")


def answer_slow(question: str):
    """音箱那一跳已经回过垫词了，答案晚点到：豆包的声音从电视喇叭出来。

    只打豆包一次，跑完顺手写进缓存 —— 下次再问同一句就是音箱秒答。
    """
    if targets.LLM_OFF:
        log(f"问「{question}」→ 豆包已阻断，没打外网")
        return
    text, mp3 = targets.llm_say(question)
    if not text:
        log(f"问「{question}」→ 豆包没答上")
        return
    targets.remember(question, text)
    ok, info = targets.say_on_tv(mp3)
    if ok:
        mark_sent(question)   # 电视已经说过了，之后再喊「继续回答」不该再来一遍
    log(f"问「{question}」→ 「{text}」 电视出声={ok} {info[:80]}")
    record({"说": question, "结果": ("电视说了：" + text) if ok else ("电视没出声，答案存着：" + text)})


def handle(req: dict, forced: str = ""):
    intent = req.get("intentName") or ""
    utterance = req.get("utterance") or ""
    scheme = forced or ("nlu" if intent in DEFAULT_INTENTS else "corpus")
    t0 = time.time()
    pick = menu_pick(req)
    if pick and pick != utterance:
        log(f"[select] 下标对上：「{utterance}」→「{pick}」")
        utterance = pick
        req = dict(req, utterance=pick,
                   slotEntities=[dict(e, standardValue=pick, originalValue=pick)
                                 for e in (req.get("slotEntities") or [])])
    if scheme == "corpus":
        p = plan.from_slots(req)
    else:
        p = plan.from_utterance(utterance)

    if is_continue_ask(utterance):
        # 「继续回答」这类话：把上一条慢问题（存盘的）从答案库里捞出来秒答。
        q = last_q()
        ans = targets.offline_answer(q) if q else ""
        if ans and q == _read(SENT_Q_FILE):
            # 一句答案只播一次：电视或音箱已经说过一遍了，再喊就不重复了
            log(f"[{scheme}] 「{utterance}」→ 上一条「{q}」已经念过，不重复")
            return aligenie.reply("这条刚才已经说过啦，问下一个吧。",
                                  result_type="RESULT"), scheme
        if ans:
            mark_sent(q)
            log(f"[{scheme}] 「{utterance}」→ 续答上一条「{q}」(秒答)，{time.time() - t0:.2f}s")
            return aligenie.reply(ans, result_type="RESULT"), scheme
        say = "上一条还在查呢，稍等我一下。" if q else "没有要续的上一条问题。"
        log(f"[{scheme}] 「{utterance}」→ 续答落空（上一条={'有' if q else '无'}，答案={'有' if ans else '无'}）")
        return aligenie.reply(say, result_type="RESULT"), scheme

    if is_menu_ask(utterance):
        # 「你能干什么」这种话就是要点菜：候选由服务端当场给，不用提前灌实体
        body, what = menu_reply(intent, req)
        if not body:
            log(f"[{scheme}] 「{utterance}」→ {what}，退回原答案")
        else:
            log(f"[{scheme}] 「{utterance}」intent={intent or '-'} → {what}(SELECT)，{time.time() - t0:.2f}s")
            return body, scheme
    elif req.get("selectIndexList") and not pick:
        log(f"[{scheme}] 「{utterance}」点菜第二轮没对上候选")

    if p.action == "greet":
        # 光喊调用词就是「我在听」：反问一句并开麦，下一句原话还会 POST 回来。
        # 别在这里塞菜单——那是备料阶段的兜底，喊一次就给一份死名单很蠢。
        log(f"[{scheme}] 「{utterance}」intent={intent or '-'} → greet(ASK_INF)，{p.speak}")
        return aligenie.reply(p.speak, result_type="ASK_INF"), scheme
    again = False
    if p.action == "ask":
        spoken = targets.offline_answer(p.query)
        what = "ask-cache(音箱秒答)" if spoken else "ask-tv(电视说)"
        # 秒答才续麦：答完立刻开麦问下一句，平台的「问题」槽位才会被新句子覆盖掉。
        # 走电视那条不能续，开麦时电视正在播豆包的答案，会被它自己听见。
        again = bool(spoken)
        if not spoken:
            # 平台只等 1.5~2 秒（实测），豆包要 5~20 秒：这一跳不可能等到，先应下来。
            # 把这句原话存成「上一条」，答案后台算好入库后，下次喊「继续回答」就能秒答。
            remember_q(p.query)
            stuck = targets.llm_blocked()
            spoken = (stuck + "，晚点再说给你。") if stuck else "我查一下啊。"
            threading.Thread(target=answer_slow, args=(p.query,), daemon=True).start()
    else:
        spoken, what = dispatch(p)
        if what == "play-fail":
            # 没片源别说「这就放」，改让大模型讲两句
            p.speak = "没找到片源，我讲给你听啊。"
            remember_q(p.query or p.title)
            threading.Thread(target=answer_slow, args=(p.query or p.title,), daemon=True).start()
            spoken, what = p.speak, "ask-tv(无片源)"

    took = time.time() - t0
    log(f"[{scheme}] 「{utterance}」intent={intent or '-'} → {what}，{spoken} 用时{took:.2f}s")
    if took > FAST_BUDGET:
        log(f"  警告：这一跳 {took:.1f}s，超过 {FAST_BUDGET}s，平台可能已经不等了")
    return aligenie.reply(spoken or "嗯。", result_type="ASK_INF" if again else "RESULT"), scheme


def local(text: str) -> dict:
    """Mac 上我们自己的耳朵送来的整句。没有平台替我们念，所以该出声的得自己出声。"""
    p = plan.from_utterance(text)
    if p.action == "greet":       # 「您说。」是给平台开麦用的，本地耳朵不需要
        return {"干": "greet", "念": ""}
    if p.action == "ask":
        spoken = targets.offline_answer(p.query)
        if spoken:
            threading.Thread(target=targets.speak_on_screen, args=(spoken,), daemon=True).start()
            record({"说": text, "结果": "本地秒答：" + spoken})
            log(f"[local] 「{text}」→ ask-cache，{spoken}")
            return {"干": "ask-cache", "念": spoken}
        threading.Thread(target=answer_slow, args=(p.query,), daemon=True).start()
        log(f"[local] 「{text}」→ ask-tv(豆包)")
        return {"干": "ask-tv", "念": "我查一下啊。"}
    spoken, what = dispatch(p)
    if p.action != "play" and spoken:   # 放片子那条电视自己会响，别再叠一层人声
        threading.Thread(target=targets.speak_on_screen, args=(spoken,), daemon=True).start()
    log(f"[local] 「{text}」→ {what}，{spoken}")
    return {"干": what, "念": spoken}


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send(self, code, body: bytes, ctype="application/json; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _text(self, code, body: str):
        self._send(code, body.encode("utf-8"), "text/plain; charset=utf-8")

    def do_GET(self):  # noqa: N802
        path = self.path.split("?")[0]
        if path.endswith(".txt") and (path.startswith("/aligenie/") or "/" not in path[1:]):
            name = os.path.basename(path)
            f = os.path.join(VERIFY_DIR, name)
            ok = os.path.isfile(f) and os.path.realpath(f).startswith(os.path.realpath(VERIFY_DIR))
            log(f"← GET {path} 认证文件 {'给了' if ok else '本地没有'} from {self.client_address[0]}")
            if ok:
                with open(f, "rb") as fh:
                    self._send(200, fh.read(), "text/plain; charset=utf-8")
            else:
                self._text(404, "no such file")
            return
        if path in ("/health", "/aligenie/health"):
            self._send(200, json.dumps({"ok": True, "pid": os.getpid()}, ensure_ascii=False).encode())
            return
        if path == "/status":
            def a(u):
                return targets.alive(u) if LAN_CHECK else "没探"
            out = {"ok": True, "port": PORT, "dry": os.environ.get("GENIE_DRY") == "1",
                   "player": {"url": targets.PLAYER_URL, "alive": a(targets.PLAYER_URL)},
                   "screen": {"url": targets.SCREEN_URL, "alive": a(targets.SCREEN_URL)},
                   "llm": {"url": targets.LLM_URL, "alive": a(targets.LLM_URL)},
                   "recent": list(PENDING)[:20]}
            self._send(200, json.dumps(out, ensure_ascii=False).encode())
            return
        if path in ("/log", "/aligenie/log"):
            with LOCK:
                lines = list(LOG)[:80]
            self._text(200, "\n".join(lines) or "还没有请求")
            return
        if path in ("/", "/index.html"):
            with LOCK:
                rows = "".join(
                    f"<li>{html.escape(str(e.get('说','')))} <b>→</b> {html.escape(str(e.get('结果','')))}</li>"
                    for e in list(PENDING)[:20])
            st, why = targets.llm_state(), targets.llm_blocked()
            gate = ("<li><b style='color:#c00'>测试期已阻断（GENIE_LLM_OFF=1）</b>，一条外网都没打</li>"
                    if targets.LLM_OFF else "")
            if why:
                tip = ("要真人点图：Mac 上跑 <code>ssh -L 9090:127.0.0.1:9090 root@192.168.5.42</code>，"
                       "再开 <code>http://127.0.0.1:9090/v1/ops</code>"
                       if st.get("verify_pending") else "限流多半几十分钟自己过去，先不用管")
                why = (f"<li>豆包 <b style='color:#c00'>{html.escape(why)}</b>："
                       f"{html.escape(str(st.get('last_error') or ''))[:160]}"
                       f"<br><small>{tip}</small></li>")
            else:
                why = (f"<li>豆包 正常（登录态还剩 {html.escape(str(st.get('days_left', '?')))} 天，"
                       f"来源 {html.escape(str(st.get('source') or '?'))}，"
                       f"cookie {st.get('cookies', '?')} 个）</li>")
            body = ("<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width'>"
                    "<meta http-equiv=refresh content=5>"
                    "<h3>天猫精灵这边听到的</h3><ul>" + (rows or "<li>还没有句子</li>") + "</ul>"
                    "<h3>服务</h3><ul><li>盒子曲库 " + targets.PLAYER_URL + "</li>"
                    "<li>手机屏幕 " + targets.SCREEN_URL + "</li><li>大模型 " + targets.LLM_URL + "</li>"
                    + gate + why + "</ul>")
            self._send(200, body.encode(), "text/html; charset=utf-8")
            return
        log(f"← GET {path} 没这个口")
        self._text(404, "没有这个口")

    def do_POST(self):  # noqa: N802
        path = self.path.split("?")[0]
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""
        log(f"← POST {path} {n}B {raw[:1200].decode('utf-8', 'replace')}")
        if path in ("/", "") or path.startswith("/aligenie/"):
            req = aligenie.parse(raw)
            if req is None:
                self._text(400, "不是 json")
                return
            forced = {"corpus": "corpus", "nlu": "nlu"}.get(path.rsplit("/", 1)[-1], "")
            try:
                body, _ = handle(req, forced)
            except Exception as e:  # noqa: BLE001 - 绝不能让音箱听到 500
                log(f"炸了：{type(e).__name__}: {e}")
                body = aligenie.reply("我这会儿有点毛病，回头再说。", execute_code="EXECUTE_ERROR")
            self._send(200, body)
            return
        if path == "/teach":
            # 喂一批句子进答案库（不出声）：json 数组或一行一条
            try:
                qs = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                qs = raw.decode("utf-8", "replace").splitlines()
            if isinstance(qs, dict):
                qs = qs.get("问题") or qs.get("q") or []
            qs = [str(x).strip() for x in qs if str(x).strip()][:60]
            threading.Thread(target=teach, args=(qs,), daemon=True).start()
            self._send(200, json.dumps({"在灌": len(qs)}, ensure_ascii=False).encode())
            return
        if path == "/local":
            try:
                text = (json.loads(raw.decode("utf-8")).get("说") or "").strip()
            except (ValueError, UnicodeDecodeError):
                text = raw.decode("utf-8", "replace").strip()
            if not text:
                self._text(400, "没带句子")
                return
            try:
                out = local(text)
            except Exception as e:  # noqa: BLE001
                log(f"本地链炸了：{type(e).__name__}: {e}")
                out = {"干": "error", "念": "", "错": str(e)[:120]}
            self._send(200, json.dumps(out, ensure_ascii=False).encode())
            return
        if path == "/test":
            fake = {"utterance": raw.decode("utf-8", "replace").strip() or "给我看一下初一的数学视频",
                    "intentName": "", "slotEntities": []}
            body, _ = handle(fake, "nlu")
            self._send(200, body)
            return
        self._text(404, "没有这个口")

    def log_message(self, *a):
        pass


class Server(socketserver.ThreadingMixIn, socketserver.TCPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 64


if __name__ == "__main__":
    os.makedirs(targets.DATA_DIR, exist_ok=True)
    log(f"genied :{PORT} dry={os.environ.get('GENIE_DRY')} player={targets.PLAYER_URL} "
        f"screen={targets.SCREEN_URL} llm={targets.LLM_URL}")
    Server(("0.0.0.0", PORT), H).serve_forever()
