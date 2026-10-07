"""这台盒子对外喊话的地方：自己接的电视、自己的豆包门、局域网那几台播放机。

地址全部走环境变量，部署时不改代码。
"""
from __future__ import annotations

import base64
import json
import os
import re
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

PLAYER_URL = os.environ.get("PLAYER_URL", "http://192.168.5.233:7791")      # NAS playerd，认 query
SCREEN_URL = os.environ.get("SCREEN_URL", "http://192.168.5.135:7791")       # 小米4C phoned，只吃 url
TV_URL = os.environ.get("GENIE_TV_URL", "")                                  # 盒子自己的 HDMI（migpt 那个 7799/tv）
LLM_URL = os.environ.get("LLM_URL", "http://127.0.0.1:7790/v1")              # 盒子自己的 llm-bridge
LLM_OFF = os.environ.get("GENIE_LLM_OFF", "") == "1"      # 测试期间一键断豆包
LLM_TIMEOUT = float(os.environ.get("GENIE_LLM_TIMEOUT", "45"))                # 同步那一跳最多等多久
LLM_KEY_FILE = os.environ.get("LLM_KEY_FILE", "/etc/genie/llm.key")
LLM_MODEL = os.environ.get("LLM_MODEL", "doubao")
DRY = os.environ.get("GENIE_DRY") == "1"
DATA_DIR = os.environ.get("GENIE_DATA", "/var/lib/genie")
# 答案晚到时从电视喇叭出来：42 这台的 HDMI 声卡是 card0
TV_AUDIO_DEV = os.environ.get("GENIE_TV_AUDIO", "plughw:0,0")
TV_VOLUME = os.environ.get("GENIE_TV_VOLUME", "85")
SAY_LOCK = threading.Lock()

_cache = {}

# 局域网的机器绝不能走系统代理（挂了 Clash 时 urllib 会被 127.0.0.1:7890 截走，直接 502）
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _open(req, timeout):
    return _opener.open(req, timeout=timeout)


def _post(base: str, path: str, payload: dict, timeout: float = 3.0):
    req = urllib.request.Request(base.rstrip("/") + path,
                                 data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with _open(req, timeout) as r:
        return r.status, r.read()[:2000]


def _get(base: str, path: str, timeout: float = 2.5):
    with _open(urllib.request.Request(base.rstrip("/") + path), timeout=timeout) as r:
        return r.status, r.read()[:2000]


def call(base: str, path: str, payload=None, timeout: float = 4.0):
    """返回 (ok, 详情)。手机掉了、NAS 没开，都只是 ok=False，不让音箱听到错误码。"""
    if DRY:
        return True, f"dry-run {base}{path} {json.dumps(payload, ensure_ascii=False)}"
    try:
        if payload is None:
            code, body = _get(base, path, timeout)
        else:
            code, body = _post(base, path, payload, timeout)
        return 200 <= code < 300, body.decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001 - 网络层什么都能抛
        return False, f"{type(e).__name__}: {e}"


def reach(base: str, timeout: float = 1.5) -> bool:
    """在不在 LAN 同一段。不在就别浪费时间连它。"""
    key = "r:" + base.rstrip("/")
    hit = _cache.get(key)
    if hit and time.time() - hit[1] < 60:
        return hit[0]
    host = base.split("//", 1)[-1].split("/")[0].split(":")[0]
    if host in ("127.0.0.1", "localhost", "::1"):      # 本机那台不用看网段
        return True
    ok = host.count(".") == 3 and host.rsplit(".", 1)[0] == _my_prefix()
    _cache[key] = (ok, time.time())
    return ok


def _my_prefix() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("192.168.5.1", 1))
        ip = s.getsockname()[0]
        s.close()
        return ip.rsplit(".", 1)[0]
    except Exception:  # noqa: BLE001
        return ""


def alive(base: str, ttl: float = 20.0) -> bool:
    """探一下在不在。回了 401/404 也算在 —— 端口有人应答就说明机器开着。

    机器不在这个网段时直接说不在，省掉那一秒多的连接超时。
    """
    key = base.rstrip("/")
    hit = _cache.get(key)
    if hit and time.time() - hit[1] < ttl:
        return hit[0]
    if not reach(base):
        _cache[key] = (False, time.time())
        return False
    ok = False
    try:
        _get(base, "/health", 0.8)
        ok = True
    except urllib.error.HTTPError:
        ok = True
    except Exception:  # noqa: BLE001
        pass
    _cache[key] = (ok, time.time())
    return ok


def _screen_video(url: str, title: str, secs: int):
    return call(SCREEN_URL, "/play", {"url": url, "title": title, "secs": secs})


SHARE_RE = re.compile(r"(?:https?://)?[\w.-]*douyin\.com/(?:share/)?video/(\d+)")
SHORT_RE = re.compile(r"v\.douyin\.com/[\w-]+")
UA = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/537.36"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a):
        return None


def _short_video_id(short: str) -> str:
    """豆包给的都是 v.douyin.com/abcd 这种短链，视频号藏在它 302 的 Location 里。"""
    req = urllib.request.Request("https://" + short, headers={"User-Agent": UA})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    try:
        opener.open(req, timeout=12).close()
    except urllib.error.HTTPError as e:
        loc = e.headers.get("Location") or ""
        m = re.search(r"/video/(\d+)", loc) or re.search(r"modal_id=(\d+)", loc)
        return m.group(1) if m else ""
    except Exception:  # noqa: BLE001 - 断网/超时都算没找到片
        return ""
    return ""


def find_share(text: str) -> str:
    """从豆包的回答里捞出那条抖音链接，补成 7799/tv 认的完整形式。"""
    t = text or ""
    m = SHARE_RE.search(t) or SHORT_RE.search(t)
    vid = m.group(1) if m and m.groups() else _short_video_id(m.group(0)) if m else ""
    return f"https://www.iesdouyin.com/share/video/{vid}" if vid else ""


def tv_play(share: str):
    """放到这台盒子自己接的电视上（走盒子上那条现成的 7799/tv：停 karaoke 抢 DRM，放完还回去）。"""
    if not TV_URL:
        return False, "这台没接电视"
    return call(TV_URL, "?url=" + urllib.parse.quote(share, safe=""), timeout=90.0)


def cards(timeout: float = 6.0) -> list:
    """豆包这一轮附带的视频卡片 —— 和 MiGPT 播片用的是同一批，不是每轮都有。"""
    base = LLM_URL.split("/v1")[0]
    if not reach(base):
        return []
    req = urllib.request.Request(base + "/v1/cards", headers={"Authorization": "Bearer " + _key()})
    try:
        with _open(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8")).get("cards") or []
    except Exception:  # noqa: BLE001
        return []


def tv_search_play(query: str):
    """豆包要找片子 → 认它给的抖音链接 → 上电视。返回 (ok, 详情)。

    它常给一条已经失效的短链（302 回首页），所以换句话再要一次，最多两次。
    """
    for ask in (f"推荐一个{query}的抖音视频，只给链接", f"{query}的视频"):
        links = llm(ask, use_hist=False) + " " + " ".join(str(c.get("url") or "") for c in cards())
        share = find_share(links)
        if share:
            ok, info = tv_play(share)
            return ok, share + " " + info[:120]
    return False, "豆包两次都没给能解开的抖音链接"


def play_video(query: str, title: str, url: str = "", secs: int = 600):
    """先挑还活着的机器再发，别等超时：探一次 0.8s，闷头发要 3s。

    NAS 的 playerd 认词能搜；手机那块 phoned 只认现成地址。
    """
    live_player = alive(PLAYER_URL)
    live_screen = alive(SCREEN_URL)
    if url:
        if live_screen:
            ok, info = _screen_video(url, title, secs)
            if ok:
                return True, SCREEN_URL, info
        if live_player:
            ok, info = call(PLAYER_URL, "/play", {"url": url, "title": title, "secs": secs})
            if ok:
                return True, PLAYER_URL, info
        return False, "", ("没接上能出画面的机器" if not (live_screen or live_player) else info[:80])
    if live_player:
        ok, info = call(PLAYER_URL, "/play", {"query": query, "title": title, "secs": secs})
        if ok:
            return True, PLAYER_URL, info
    # 手机只吃现成地址，没片源就是没放成 —— 别调 /frame 装作放了
    if not (live_player or live_screen):
        return False, "", "两台都没开"
    return False, "", "没有能放的片源（手机只认现成地址）"


def play_audio(query: str):
    return call(PLAYER_URL, "/play", {"query": query})


def _remotes(path: str, payload=None):
    """发给还活着的手机/NAS。"""
    return [call(b, path, payload) for b in (PLAYER_URL, SCREEN_URL) if alive(b)]


def _tv_base() -> str:
    return TV_URL.split("/tv")[0] if TV_URL else ""


def tv_alive() -> bool:
    """接电视那台在不在，派活前先探一把。"""
    return bool(_tv_base()) and alive(_tv_base())


BRIDGE_STATE = os.environ.get("GENIE_BRIDGE_STATE", "http://127.0.0.1:9090/v1/ops/state")


def llm_state(ttl: float = 10.0) -> dict:
    """豆包那条腿自己的说法：限流码、还剩几天登录态、有没有要真人点的验证图。

    0.6 秒拿不到就当没有 —— 不能拖累音箱那一跳。
    """
    hit = _cache.get("llmstate")
    if hit and time.time() - hit[1] < ttl:
        return hit[0]
    st = {}
    try:
        st = json.loads(_get(BRIDGE_STATE, "", 0.6)[1].decode("utf-8"))
    except Exception:  # noqa: BLE001 - 运维口没起就当正常，别为这个改口
        st = {}
    _cache["llmstate"] = (st, time.time())
    return st


def llm_blocked() -> str:
    """不通就回一句给人听的原因，通的话回空串。"""
    st = llm_state()
    err = str(st.get("last_error") or "")
    if st.get("verify_pending"):
        return "豆包要人工验证"
    if "710022" in err:
        return "豆包被限流了"
    return ""


def control(action: str):
    """停/下一首/音量：谁活着发给谁，电视那条一起发（它自己会停 7799 的片）。"""
    path = {"stop": "/stop", "next": "/next", "prev": "/replay"}.get(action)
    if not path:
        return False, "不认识的动作 " + action
    payload = {} if action != "prev" else {"n": 1}
    outs = _remotes(path, payload)
    if _tv_base():
        outs.append(call(_tv_base(), path))
    if not outs:
        return False, "两台都没开"
    return any(ok for ok, _ in outs), " ".join(i[:60] for _, i in outs)


def volume(delta: int):
    outs = _remotes("/volume", {"n": delta})
    if any(ok for ok, _ in outs):
        return True, " ".join(i[:60] for ok, i in outs if ok)
    if _tv_base():
        # 电视上放的片子音量归 7799 管，曲库那台要么没开要么不认这个口
        return call(_tv_base(), "/volume", {"n": delta})
    return outs[0] if outs else (False, "没有能调音量的")


def speak_on_screen(text: str, with_voice: bool = True):
    """大模型答得慢时，答案不走音箱，走手机喇叭/字幕。"""
    return call(SCREEN_URL, "/say", {"who": "精灵", "text": text, "speak": with_voice})


def _key() -> str:
    try:
        with open(LLM_KEY_FILE, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


# 答案库：盒子自己带一份，Mac 不在也能答；豆包每答一条就追加进来，越用越全
OFFLINE_DIR = os.environ.get("GENIE_OFFLINE_DIR", "")
_kb = None
_kb_mtime = 0.0
_kb_lock = threading.Lock()


def _norm(q: str) -> str:
    return re.sub(r"[\s，。？！?!,.]+", "", q or "")


# 「讲讲X的历史」和「X的故事」和「介绍一下X」问的是同一件事：把这类前台词剥掉，
# 一条答案能顶好几种问法，不然 357 个话题 × 35 种句式得灌一万多条。
_LEAD = re.compile(r"^(能不能|能|可以|帮我|给我|请|我想|我要)?(来一段|讲讲看|讲讲|说说|说一说|介绍一下|介绍|了解|听听|听|看看|看|放|讲|说)+")
_TAIL = re.compile(r"(的|了)?(小故事|故事|历史|知识|来历|由来|简介|是谁|是什么|怎么来的|能放|能看|可以看|可以放|想看|要看|吗|吧|呀|啊|呢)+$")
_core_kb: dict = {}


def _core(q: str) -> str:
    s = _TAIL.sub("", _LEAD.sub("", _norm(q)))
    return s or _norm(q)


def _core_index() -> dict:
    """按主题词建一遍索引（库变了才重建，几毫秒）。"""
    global _core_kb
    kb = _load_kb()
    if len(_core_kb) != len(kb):
        _core_kb = {}
        for k, v in kb.items():
            _core_kb.setdefault(_core(k), v)
    return _core_kb


def _kb_path() -> str:
    return os.path.join(OFFLINE_DIR, "answers.json")


def _load_kb():
    # 按 mtime 判断要不要重读：批量预热是另起的脚本往 answers.json 里补货的，
    # 只缓存第一次读的话，灌完不重启服务就一条也认不出来。
    global _kb, _kb_mtime
    try:
        m = os.path.getmtime(_kb_path())
    except OSError:
        m = 0.0
    if _kb is not None and m == _kb_mtime:
        return _kb
    _kb = {}
    try:
        with open(_kb_path(), encoding="utf-8") as f:
            data = json.load(f)
        _kb = {k: v for k, v in data.items() if isinstance(v, str)}
    except Exception:  # noqa: BLE001 - 没灌过就是空的
        pass
    _kb_mtime = m
    return _kb


_JUNK = re.compile("[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U00002B00-\U00002BFF"
                   "‍⁯#*>`~|•·■□●]")
# 模型把话头甩回来的那些尾巴，一句之内出现任一就算它在等人接话
ASKBACK = _ASKBACK = re.compile("要不要|需不需要|还用不用|想不想|还想|我可以帮|还有什么"
                      "|你来|你想让|让我原样|复述|出几道|考考你|下一题|继续讲|还想听|要不要接着")


def _plain(t: str) -> str:
    """模型爱写 markdown、爱加 emoji、爱列 1.2.3.，音箱会把「井号」「对勾」念出来，还会念四十秒。收成能念的话。"""
    lines = [l.strip() for l in (t or "").splitlines()]
    body = [l for l in lines if l and not l.startswith(("#", ">", "-", "*", "|", "（适合"))]
    # 第一行常是「唐朝小故事（适合小朋友）」这种标题，念出来是废话
    if len(body) > 1 and len(body[0]) <= 16 and not re.search(r"[。！？]", body[0]):
        body = body[1:]
    body = [re.sub(r"^\d+\s*[.、)）]\s*", "", l) for l in body]
    # 行和行之间补「。」，不然上下两行粘死，「减二百五十二」这种数字会被念错
    s = _JUNK.sub("", "。".join(body))
    # 豆包答题常顺手甩一条抖音链接，音箱会照着念「https 点点」：网址一律剔掉，只留人话
    s = re.sub(r"https?://[A-Za-z0-9./?=&%_#~+-]+", "", s)
    # 「角色介绍：」这类小标题下面的正文常被剥光，剩个光秃秃的冒号，音箱念着像卡壳
    s = re.sub(r"[^。！？；，]{1,12}：(?=[。！？；，]|$)", "", s)
    s = re.sub(r"([。！？；，])\1+", r"\1", s)  # 两行都带句号时会叠成「。。」，TTS 听着卡顿
    # 「什么？。」是上下两行粘出来的：逗号让位给句号，问号叹号后面不叠第二个
    s = re.sub(r"[，、](?=[。！？；])", "", s)
    s = re.sub(r"([。！？；])[。！？；，]+", r"\1", s)
    # 音箱念「斜杠」「破折号」「摄氏度符号」都会卡，换成能读的字
    s = s.replace("℃", "摄氏度")
    s = re.sub(r"(?<=\d)[/／](?=\d)", "到", s)
    s = re.sub(r"[–—~]", "到", s)
    s = s.replace("/", "、").replace("／", "、")
    # % 得整个挪到数字前面才念得对（99%→百分之99）；×÷ 换字。
    # 但 `+` 和数字中间的 `-` 故意不动：「常规+加时」「AI小兵+其他玩家」「4-1击败」
    # 念成「加」「和」「比」才对，代码分不出来，这种得回库里重问。
    s = re.sub(r"(\d+(?:\.\d+)?)\s*[%％]", r"百分之\1", s)
    s = s.replace("×", "乘").replace("÷", "除")
    s = re.sub(r"\s+", "", s)
    # 豆包爱把话头甩回来（「要不要我给你一句简短的，你来让我原样复述？」），
    # 音箱念出来像在等人接话。只丢尾巴，整条都是反问的留着，别把答案清空。
    segs = [x for x in re.split(r"(?<=[。！？；])", s) if x]
    while len(segs) > 1 and _ASKBACK.search(segs[-1]):
        segs.pop()
    s = "".join(segs) or s
    out = ""
    for seg in re.split(r"(?<=[。！？；])", s):
        if not seg:
            continue
        if out and len(out) + len(seg) > 80:
            break
        out += seg
    out = out or s
    if len(out) > 80:
        cut = out[:80]
        heads = [i for i, ch in enumerate(cut) if ch in "。！？；，"]
        if heads:
            k = heads[-1] + 1
            if len(cut) - k < 10 and len(heads) > 1:
                k = heads[-2] + 1      # 残尾只剩几个字，整截丢掉
            out = cut[:k]
        else:
            out = cut
    if out.count("《") > out.count("》"):  # 别断在书名号中间
        out = out[:out.rfind("《")]
    while out.count("（") > out.count("）"):  # 括号截一半，音箱念个「左括号」就没了下文
        out = out[:out.rfind("（")]
    out = out.strip().rstrip("，、：；")
    if out and out[-1] not in "。！？…":
        out += "。"
    return out.lstrip("。，、：；")


# 答案库是存死的，天气/新闻存一次就错一天，日历存一次就错一年
_STALE_Q = re.compile(r"天气|气温|多少度|新闻|最新")


def _clock_answer(question: str) -> str:
    """问今天/问现在的，当场算，别查库。盒子只有 UTC，家里说的是北京时间，自己 +8。

    只认带「今天/现在/此刻/今日」的：「重阳节是几月几号」「电影几点开始」答成今天就是胡说。
    """
    if not re.search(r"今天|今晚|此刻|现在|今日", question):
        return ""
    if re.search(r"明天|后天|大后天|下周|上周|明年|去年", question):
        return ""
    t = time.gmtime(time.time() + 8 * 3600)
    day = "一二三四五六日"[t.tm_wday]
    head = "礼拜" if "礼拜" in question else "星期"
    if "几点" in question:
        return f"现在{t.tm_hour}点{t.tm_min}分。"
    if "几号" in question or "日期" in question:
        return f"今天是{t.tm_mon}月{t.tm_mday}号，{head}{day}。"
    if "星期" in question or "礼拜" in question:
        return f"今天{head}{day}。"
    return ""


def offline_answer(question: str) -> str:
    now = _clock_answer(question)
    if now:
        return now
    if _STALE_Q.search(question):
        # 天气/新闻这类宁可每次现问，也别把去年存的念给孩子听
        return ""
    if not OFFLINE_DIR:
        return ""
    q = _norm(question)
    if not q:
        return ""
    with _kb_lock:
        kb = _load_kb()
        hit = kb.get(q) or _core_index().get(_core(q))
        if hit:
            return _plain(hit)
        for k, v in kb.items():
            if len(k) >= 5 and (k in q or q in k):
                return _plain(v)
    return ""


def remember(question: str, answer: str) -> str:
    """存进答案库，返回真正会念出来的那句（没存成就是空串）。"""
    if not OFFLINE_DIR or not answer:
        return ""
    if _clock_answer(question) or _STALE_Q.search(question):
        return ""        # 当场算的、隔天就旧的，存下来就是留一条过期答案
    q = _norm(question)
    if not q:
        return ""
    clean = _plain(answer)
    if not clean:  # 剔完网址什么都不剩，存进去等于让音箱念一串乱码
        return ""
    if len(clean) < 30 and clean.endswith(("？", "?")) and ASKBACK.search(clean):
        return ""      # 整条就是个反问，不入库
    with _kb_lock:
        kb = _load_kb()
        try:
            os.makedirs(OFFLINE_DIR, exist_ok=True)
            tmp = _kb_path() + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({**kb, q: clean}, f, ensure_ascii=False, sort_keys=True)
            os.replace(tmp, _kb_path())
            kb[q] = clean
            return clean
        except OSError:
            return ""


_HIST: list = []          # 全家一个对话：最近几轮，追问才接得上
_HIST_LOCK = threading.Lock()
HIST_MAX = 12


def history_reset():
    with _HIST_LOCK:
        _HIST.clear()


def _chat(prompt: str, system: str, timeout: float, want_audio: bool = False,
          use_hist: bool = True):
    """打一次豆包，拿回 message dict（要了语音就多个 audio 字段）。不通就 None。

    默认带上最近几轮，所以「那它呢」「再细说」这种追问能接上同一个对话。
    """
    if LLM_OFF:
        return None
    with _HIST_LOCK:
        tail = _HIST[-HIST_MAX:] if use_hist else []
    msg = ([{"role": "system", "content": system}] if system else []) + \
          tail + [{"role": "user", "content": prompt}]
    if not reach(LLM_URL):
        return None
    body = {"model": LLM_MODEL, "messages": msg, "stream": False,
            "max_tokens": 800 if want_audio else 1500}
    if want_audio:
        body["audio"] = {"format": "mp3"}
        body["user"] = "genied-tv"
    req = urllib.request.Request(
        LLM_URL.rstrip("/") + "/chat/completions",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + _key()},
        method="POST")
    try:
        with _open(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
        m = data["choices"][0]["message"]
    except Exception:  # noqa: BLE001
        return None
    if use_hist and (m.get("content") or "").strip():
        with _HIST_LOCK:
            _HIST.append({"role": "user", "content": prompt})
            _HIST.append({"role": "assistant", "content": m["content"]})
            del _HIST[:-HIST_MAX]
    return m


def llm(prompt: str, timeout: float = LLM_TIMEOUT, system: str = "",
        use_hist: bool = True) -> str:
    m = _chat(prompt, system, timeout, use_hist=use_hist)
    return ((m or {}).get("content") or "").strip()


def llm_say(prompt: str, system: str = "", timeout: float = LLM_TIMEOUT):
    """一次请求同时拿回文本和豆包自己的声音。音频落成本地 mp3，返回 (文本, 路径)。

    豆包爱写 markdown，但那是它自己的话：只在屏幕字幕和答案库里清掉，
    不再另发一次请求让它「原样念一遍」——那等于往账号里塞指令，还多等一倍。
    """
    m = _chat(prompt, system, timeout, want_audio=True)
    if not m:
        return "", ""
    return (m.get("content") or "").strip(), _mp3(m)


def _mp3(m) -> str:
    data = ((m or {}).get("audio") or {}).get("data")
    if not data:
        return ""
    path = os.path.join(DATA_DIR, f"说-{int(time.time() * 1000)}.mp3")
    try:
        with open(path, "wb") as f:
            f.write(base64.b64decode(data))
    except OSError:
        return ""
    return path


def say_on_tv(mp3: str) -> tuple[bool, str]:
    """答案晚到时不走音箱，走这台盒子接的电视喇叭（HDMI 声卡）。放完就删。"""
    if not mp3 or not os.path.exists(mp3):
        return False, "没拿到豆包的音频"
    cmd = ["mpv", "--no-video", f"--audio-device=alsa/{TV_AUDIO_DEV}",
           "--really-quiet", f"--volume={TV_VOLUME}", mp3]
    with SAY_LOCK:
        try:
            p = subprocess.run(cmd, timeout=120, capture_output=True)
        except (OSError, subprocess.TimeoutExpired) as e:
            return False, f"{type(e).__name__}: {e}"
    return p.returncode == 0, (p.stderr or b"").decode("utf-8", "replace")[-120:]

