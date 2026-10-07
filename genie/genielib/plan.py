"""意图&参数 → 要干什么。

两条路都归到这里：
  方案A 天猫精灵自己解析好，把 intentName + slotEntities 给我们；
  方案B 天猫精灵只把原话 utterance 丢过来（默认意图），自己按词表判。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

GRADES = ["学前", "一年级", "二年级", "三年级", "四年级", "五年级", "六年级",
          "初一", "初二", "初三", "高一", "高二", "高三"]
SUBJECTS = ["数学", "语文", "英语", "科学", "道德与法治", "历史", "地理",
            "生物", "物理", "化学", "政治", "美术", "音乐", "体育"]
# 他嘴上怎么说，我们就按怎么说认，不去造词
TOPICS = {
    "跳舞": ["跳舞", "舞蹈", "街舞"],
    "打篮球": ["打篮球", "篮球", "灌篮", "扣篮"],
    "三角洲": ["三角洲", "三角洲行动"],
    "炒菜": ["炒菜", "做菜", "做饭", "菜谱", "红烧肉", "家常菜"],
    "菜系": ["菜系", "吃什么", "今晚吃啥", "推荐菜"],
    "动画片": ["动画片", "动画", "卡通"],
    "笑话": ["笑话", "段子"],
}
LOOK = ["给我看", "我要看", "想看", "看一下", "来一个", "来段", "找一", "放一个",
        "放一段", "播放", "帮我放", "给我放", "推荐一", "来点", "有没有"]
# 方案A 命中了播放意图，但这句话其实是在问事：交给大模型，别去放视频
ASK_WORDS = ["怎么做", "是什么", "什么意思", "怎么学", "怎么办", "怎么样", "为什么",
             "哪些", "哪里", "几个", "多少", "的故事", "讲讲", "知识",
             "干什么", "做什么", "会什么", "有什么", "什么功能", "帮助", "你是谁",
             "讲故事", "听故事",
             # 「帮我出几道书法题」是被念出来的动词壳骗进播放腿的：出题/造句是要模型答题，不是放片
             "出题", "出一道", "出几道", "来一道", "的一道", "造句", "应用题", "判断题",
             "选择题", "填空题", "练习题", "小实验", "造个句", "造句子"]
STOP = ["别放了", "停下来", "暂停", "关掉", "不看了", "停止", "停"]
NEXT = ["下一个", "换一个", "再来一个", "下一集", "跳过"]
PREV = ["上一个", "刚才那个", "重播", "返回去"]
LOUD = ["大声", "响一点", "音量大"]
SOFT = ["小声", "轻一点", "音量小"]
# 屏归屏、音归音：带「看/视频」的送到屏，纯听的送到音箱能播的曲库
SCREEN_WORDS = ["视频", "看", "动画片", "动画", "电视", "电影", "画面"]

_HOT = ["最近很火", "最近火", "很火", "最火", "热门", "爆款", "热搜", "流行", "大家都在看"]
# 光喊技能名不算一句话，得反问一句，让下一句回到我们手里
SKILL_WORDS = ["两只小狗", "小狗汪汪", "猪八戒", "花生同学", "小狗同学", "豆包同学", "松鼠同学", "测试来电", "天猫精灵", "你好天猫", "精灵", "打开", "我要", "我想"]


@dataclass
class Plan:
    action: str                      # play topic | play url | stop | next | prev | volume | tts | noop
    speak: str = ""
    query: str = ""
    kind: str = "video"             # video | audio
    grade: str = ""
    subject: str = ""
    topic: str = ""
    url: str = ""
    title: str = ""
    hot: bool = False
    slots: dict = field(default_factory=dict)
    source: str = ""                 # corpus(方案A) / nlu(方案B)
    intent: str = ""

    @property
    def playable(self) -> bool:
        return bool(self.url or self.query or self.topic or self.grade or self.subject)


def _first(text: str, table) -> str:
    for w in table:
        if w in text:
            return w
    return ""


SLOT_ALIAS = {"年级": "grade", "学科": "subject", "话题": "topic", "问题": "query"}

# 只有这些意图是「放东西看」的语义；别的意图（如 zhubajie）带着整句话过来就是提问
PLAY_INTENTS = {"", "default", "Default", "默认意图", "unknow", "unknown", "play_video", "看视频"}


def _strip_skill(utt: str) -> str:
    t = re.sub(r"[，。！？、,.!?~\s]", "", utt or "")
    for w in SKILL_WORDS:
        t = t.replace(w, "")
    return t


def _tidy(utt: str) -> str:
    """没有槽位时，从整句话里抠出「要放的东西」：去掉语气词和动词壳。"""
    t = re.sub(r"[吗呢啊吧]+$", "", _strip_skill(utt))
    t = re.sub(r"^(能不能|可不可以|能否|能|可以|帮我|给我|我想|我要)", "", t)
    t = re.sub(r"^[放看]", "", t)
    return t or _strip_skill(utt)


# 猪八戒把整句话塞进「问题」槽，搜片时得把动词壳剥掉：「能放动画片吗」→ 动画片
_PLAY_LEAD = re.compile(r"^(能不能|可不可以|能否|可以|能|帮我|给我|我想|我要|想|要|来点|来|播放|播|放|看|听|找|打开|推荐)+")
_PLAY_TAIL = re.compile(r"[吗呢吧啊呀哦]+$")


def _search(t: str) -> str:
    """把「能放动画片吗」这种整句话剥成搜片用的词（动画片）；不像要放东西就返回空。"""
    s = _strip_skill(t)
    if _first(s, ASK_WORDS):
        return ""
    # 「猫夜里看得见吗」里有「看」但不是要放片：没动词开头、又是问句，就别搜片
    if _PLAY_TAIL.search(s) and not _PLAY_LEAD.match(s):
        return ""
    g, sub = _first(s, GRADES), _first(s, SUBJECTS)
    body = _PLAY_TAIL.sub("", _PLAY_LEAD.sub("", s))
    # 「给我出一年级的书法题」「给我讲杯弓蛇影」：动词壳一剥就露出出题/讲故事的字头，
    # 这是要模型答题不是要放片。别写宽了——「播放出山」那首歌也叫「出山」。
    if re.match(r"^出.{0,8}?[道题份套]", body) or body.startswith("讲"):
        return ""
    body = re.sub(r"^一[下段首篇个支部门]", "", body)
    if g or sub:
        core = re.sub("的|课|课程|课本", "", body.replace(g, "").replace(sub, ""))
        if len(core) < 2:
            return " ".join(x for x in [g, sub] if x)   # 「放初一的数学课」→「初一 数学」
    if _PLAY_LEAD.match(s) or _first(s, SCREEN_WORDS):
        return body or s
    return ""


# 一句短话就是遥控器按键：能整句对上才算，别把「下一个视频讲了什么」也停了
CTRL = ((STOP, "stop", "停了。"), (NEXT, "next", "换一个。"), (PREV, "prev", "回到上一条。"),
        (LOUD, "volume_up", "大声点。"), (SOFT, "volume_down", "小声点。"))


def from_slots(d: dict) -> Plan:
    """方案A：天猫精灵给的 intentName + slotEntities（standardValue 是归一化过的，用它）。"""
    slots = {}
    raw = {}
    for e in d.get("slotEntities") or []:
        name = e.get("intentParameterName") or ""
        val = e.get("standardValue") or e.get("originalValue") or ""
        base = re.split(r"[(（]", name)[0]
        base = SLOT_ALIAS.get(base, base)
        slots[base] = val
        raw[name] = e
    intent = d.get("intentName") or ""
    utt = d.get("utterance") or ""
    text = " ".join([slots.get("topic", ""), slots.get("subject", ""), slots.get("query", ""), utt])
    p = Plan(action="play", slots=slots, source="corpus", intent=intent)
    p.grade = slots.get("grade") or (_first(text, GRADES))
    p.subject = slots.get("subject") or (_first(text, SUBJECTS))
    sent = slots.get("query") if not slots.get("topic") else ""
    p.topic = slots.get("topic") or (_search(sent) if sent else "")
    if sent:
        p.grade = p.subject = ""      # 年级学科已经抠进 topic 了，别再叠一遍
    p.url = slots.get("url") or ""
    p.query = p.topic
    p.title = "".join(x for x in [p.grade, p.subject, p.topic] if x) or _tidy(utt) or utt
    if not _strip_skill(utt):
        # 会话里槽位会被保留，裸喊调用词时平台会把上一轮的问题再发一遍 → 只认这句话本身
        p.action, p.speak = "greet", "您说。"
        return p
    t = _strip_skill(utt) or _strip_skill(sent)
    if len(t) <= 6:
        for words, act, say in CTRL:
            # 单字（「停」）只认整句对上，别让「停车在哪里」把片子停了
            if t in words or _first(t, [w for w in words if len(w) > 1]):
                p.action, p.speak = act, say
                return p
    key = _search(utt or sent)
    if sent and not key:
        # 「问题」是必选参数，能带着它回来就说明这句是精灵追问收到的提问
        p.action = "ask"
        p.query = sent
        # 实体只对上前半句时槽位会被截短（说「怎么做手工贺卡」回的是「怎么做手工」），
        # 原话更长又包着这句，就按原话问，别答错题
        full = _strip_skill(utt)
        if full and full != sent and sent in full:
            p.query = full
        p.title = p.query[:20]
        p.speak = ""
        return p
    if intent not in PLAY_INTENTS and not key:
        p.action = "ask"
        p.query = _strip_skill(utt) or utt
        p.title = p.query[:20]
        p.speak = ""
        return p
    if _first(utt, ASK_WORDS) and not key:
        p.action = "ask"
        p.query = re.sub("|".join(SKILL_WORDS), "", re.sub(r"[，。！？、,.!?~\s]", "", utt)) or utt
        p.title = p.query[:20]
        p.speak = ""
        return p
    p.kind = "video" if _first(utt + p.title, SCREEN_WORDS) else _kind_by_intent(intent)
    p.speak = "这就放" + (p.title or "你要看的")
    return p


def _kind_by_intent(intent: str) -> str:
    if "听" in intent or "music" in intent or "歌" in intent:
        return "audio"
    return "video"


def from_utterance(utt: str) -> Plan:
    """方案B：只有一句原话，自己判。"""
    t = re.sub(r"[，。！？、,.!?~\s]", "", utt or "")
    p = Plan(action="noop", source="nlu")
    if not t:
        p.speak = "没听清，再说一遍。"
        return p
    for w in SKILL_WORDS:
        t = t.replace(w, "")
    if not t:
        p.action, p.speak = "greet", "您说。"
        return p
    if _first(t, STOP):
        p.action, p.speak = "stop", "停了。"
        return p
    if _first(t, NEXT):
        p.action, p.speak = "next", "换一个。"
        return p
    if _first(t, PREV):
        p.action, p.speak = "prev", "回到上一条。"
        return p
    if _first(t, LOUD):
        p.action, p.speak = "volume_up", "大声点。"
        return p
    if _first(t, SOFT):
        p.action, p.speak = "volume_down", "小声点。"
        return p

    p.grade = _first(t, GRADES)
    p.subject = _first(t, SUBJECTS)
    for canon, alts in TOPICS.items():
        if _first(t, alts):
            p.topic = canon
            break
    p.hot = bool(_first(t, _HOT))
    wants_video = bool(_first(t, SCREEN_WORDS)) or bool(_first(t, LOOK))

    # 「红烧肉怎么做」里带的红烧肉是词表里的题材，但这句话是在问事：没明说要看就不要放
    if not wants_video and _first(t, ASK_WORDS):
        p.action = "ask"
        p.query = t or utt
        p.title = p.query[:20]
        return p

    if p.grade or p.subject or p.topic:
        bits = [x for x in [p.grade, p.subject, p.topic] if x]
        s = _search(t)
        rest = s
        for b in bits:
            rest = rest.replace(b, "")
        # 「魔道祖师的动画」里「动画」只是题材词，真正要点的是整句；只有整句比题材词多出东西才用整句
        if len(rest) > 1:
            p.action, p.query, p.title = "play", s, s
            p.speak = "放" + s + "。"
            return p
        p.action = "play"
        p.kind = "video"
        p.query = " ".join(bits)
        p.title = p.query
        p.speak = ("放" + "".join(bits) + "的视频。") if wants_video else ("放" + "".join(bits) + "。")
        return p

    # 没有词表里的题材，但话头明确是要看东西（「我想看三生三世十里桃花」「放汪汪队立大功」）：照放，别去问大模型
    s = _search(t)
    if s:
        p.action, p.query, p.title = "play", s, s
        p.speak = "放" + s + "。"
        return p

    # 剩下全是开放句子（「两只小狗宇宙有多大」），交给大模型
    p.action = "ask"
    p.query = t or utt
    p.title = p.query[:20]
    p.speak = ""
    return p
