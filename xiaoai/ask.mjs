// 免嘴问一句：绕开「小爱同学」这个唤醒词，直接从本机把一句话喂进 MiGPT 的消息入口，
// 让音箱照常把答案播出来。
//
//   node ask.mjs "今天天气怎么样"        # 发一句就退
//   node ask.mjs --serve                # 常驻，收 GET http://127.0.0.1:7799/?q=...
//
// 为什么能这样：MiGPT 拿"用户说了什么"是**轮询小米云的对话历史**
// （userprofile.mina.mi.com/device_profile/v2/conversation，每 HEARTBEAT_MS 一次），
// 所以唤醒本身只能靠人嘴；但 `engine.onMessage(msg)` 就是轮询命中后要调的那个入口，
// 直接调它 = 假装"音箱刚听到这句话"，config.js 里的 onMessage → askAI →
// speaker.play({url}) 全程照走（出声走 ubus mediaplayer/player_play_url）。
//
// 心跳轮询照旧在跑，所以人和这条口子同时用不冲突；两边唯一的竞争是 lastMsg
// （后到的会把先到的回答丢掉），这是引擎本来的行为。
//
// 只绑 127.0.0.1：这台机器局域网里谁都能借音箱说话不合适，要给别人用得自己想清楚再改。
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";

const here = path.dirname(fileURLToPath(import.meta.url));
const env = (k) => {
  const m = new RegExp(`^${k}=(.*)$`, "m").exec(readFileSync(path.join(here, "migpt.env"), "utf8"));
  return m?.[1]?.trim();
};

const args = process.argv.slice(2);
const serve = args.includes("--serve");
const query = args.find((a) => !a.startsWith("--"));

if (!env("MI_PASS_TOKEN") && !env("MI_PASSWORD")) {
  console.error("❌ migpt.env 里没有 MI_PASS_TOKEN（密码那条路会被风控要验证码，别用）");
  process.exit(1);
}

// app.js 里的 MiGPT.start() 是主循环、永不 resolve，所以只能"起了但不等"，
// 再自己等 speaker 就绪（登录+拉设备列表要几秒）。
const { boot } = await import("./app.js");
const { MiGPT } = await import("@mi-gpt/next");
boot().catch((e) => {
  console.error("启动失败:", e?.message || e);
  process.exit(1);
});
for (let i = 0; i < 60 && !MiGPT.speaker; i++) await new Promise((r) => setTimeout(r, 500));
if (!MiGPT.speaker) {
  console.error("❌ 20 秒内没拿到音箱（多半是凭据或 MI_SPEAKER_NAME 不对）");
  process.exit(1);
}

const ask = async (text) => {
  await MiGPT.onMessage({
    id: `qoder-${Date.now()}`,
    sender: "user",
    deviceId: MiGPT.speaker?.device?.did ?? "qoder",
    timestamp: Date.now(),
    text,
  });
};

if (!serve) {
  if (!query) {
    console.error(`用法: node ${path.basename(fileURLToPath(import.meta.url))} "要问的话" | --serve`);
    process.exit(2);
  }
  await ask(query);
  process.exit(0);
}

// ---- 常驻控制口 -------------------------------------------------------------
// 音箱是小米云"推"命令的设备：api2.mina.mi.com/remote/ubus → mediaplayer/player_play_url。
// 所以只要能报出这条 ubus 的口，就能拿它当**纯播放器**：豆包反代给什么链接，就播什么，
// 完全不必经过唤醒词、也不必经过 askAI。@mi-gpt/next 的 MiGPT.MiNA 已经把签名那套用我们的
// passToken 做完了（getStatus/setVolume/play/pause/stop/callUbus），别自己重造。
//
// 最值钱的是 getStatus()：它回 status/volume/位置 —— 以前没法证明"真的出声了"，只能看日志猜。
//
// GET 一览：
//   /ask?q=…     走整条链（豆包回答 + 豆包的声音）
//   /status      播放状态  /messages?n=6 小米云记的对话（看谁答了什么）
//   /volume?n=20 音量  /stop  停
//   /play?url=…  只播一个已经拼好的门链接（必须是我们自己那台门，别做成开放转发器）
//   /song?q=…    豆包点歌：先问一句拿引用卡片里的直链，解成门的 /v1/play，再推给音箱
const { createServer } = await import("node:http");
const { createHmac } = await import("node:crypto");

const BASE = (env("VOICE_BASE_URL") || env("LLM_BASE_URL") || "")
  .replace(/\/v1\/?$/, "").replace(/\/+$/, "");
const KEY = env("LLM_API_KEY") || "";
const MODEL = env("LLM_MODEL") || "doubao";

// 播放器（这里是小米云）加不了 header，所以挂派生 token（llm_bridge 的 media_token）。
// 注意**一个 `&` 都不许出现**：实测小米云会把 & 变成空格再发给音箱
// （`/v1/play?url=...%20max_height=480&t=...` → 签名对不上，两次 401），所以 token 走路径段 /t/<hex>。
// 值的编码要跟 Python 的 quote(safe='') 逐字节一致，故在 encodeURIComponent 之上再补 !'()*。
const enc = (s) =>
  encodeURIComponent(s).replace(/[!'()*]/g, (c) => `%${c.charCodeAt(0).toString(16).toUpperCase()}`);

const doorUrl = (route, param, value) => {
  const resource = `${route}?${param}=${enc(value)}`;
  const tok = createHmac("sha256", KEY).update(resource).digest("hex").slice(0, 32);
  return `${BASE}${route}/t/${tok}?${param}=${enc(value)}`;
};

const call = async (route, init, ms = 20000) => {
  const res = await fetch(`${BASE}/v1/${route}`, {
    ...init,
    headers: { authorization: `Bearer ${KEY}`, "content-type": "application/json", ...init?.headers },
    signal: AbortSignal.timeout(ms),
  });
  if (!res.ok) throw new Error(`门 ${route} → ${res.status}`);
  return res.json();
};

// 豆包给的视频卡片是 iesdouyin 分享页，得让门去解成真直链；和电视那条用同一个 /v1/play。
// 卡片里也有百科/文章那种不可播的，硬推给音箱只会"命令成功但啥也没响"，所以先按能播的过一遍。
const PLAYABLE = /(iesdouyin|douyin)\.com\/share\/(video|note)\/\d+|\.(mp3|m4a|aac|mp4|wav)(\?|$)/i;

const song = async (q) => {
  await call("chat/completions", {
    method: "POST",
    // 不带 user 就是 MiGPT 用的那个 "default"：跟着音箱的对话走，省得在他账号上又开新话题
    body: JSON.stringify({ model: MODEL, messages: [{ role: "user", content: q }], stream: false }),
  }, 60000);
  const { cards = [] } = await call("cards");
  const hit = cards.find((c) => PLAYABLE.test(c.url || ""));
  if (!hit) {
    throw new Error(
      `豆包这轮没给可播的链接（${cards.length} 张卡片：${cards.map((c) => c.url).join(" , ").slice(0, 200) || "无"}）`,
    );
  }
  return { url: doorUrl("/v1/play", "url", hit.url), title: hit.title || hit.url };
};

// 播了到底响没响，只有小米云肯告诉我们：player_play_url 回 code 0 只代表命令发出去了，
// 真正出声是 status=playing。所以推完自己盯几秒，别拿"命令成功"当结论。
const watch = async (tries = 5) => {
  for (let i = 0; i < tries; i++) {
    const s = await mina()?.getStatus();
    if (s?.status === "playing") return s;
    await new Promise((r) => setTimeout(r, 1200));
  }
  return mina()?.getStatus();
};

const mina = () => MiGPT.MiNA || MiGPT.speaker?.MiNA;

const routes = {
  async ask(p) {
    const q = p.get("q");
    if (!q) throw new Error("缺少 ?q=");
    ask(q).catch((e) => console.error("回答失败:", e?.message || e));
    return { ok: true, sent: q, note: "回答是异步的，过几秒查 /status" };
  },
  async status() {
    return (await mina()?.getStatus()) ?? { error: "MiNA 还没就绪" };
  },
  // 小米云记的对话历史（只读）。为什么要：查"小爱是不是自己也答了一遍"不能靠耳朵猜，
  // 这里每条都带它自己的回复和我们那条 url，谁说了什么一目了然。
  async messages(p) {
    const raw = await mina()?.getConversations({ limit: Number(p.get("n") || 6) });
    // 小米云这个口的字段名很随性：{records:[{time, query, answers:[{type:"TTS", tts:{text}}]}]}
    const list = Array.isArray(raw) ? raw : raw?.records ?? raw?.messages ?? [];
    return list.map((m) => ({
      时间: m.time ? new Date(m.time).toLocaleTimeString("zh-CN", { hour12: false }) : "?",
      听到: m.query,
      谁答的: (m.answers ?? []).map((a) =>
        a?.tts?.text ? `小爱：${a.tts.text.slice(0, 40)}`
          : a?.content?.startsWith("http") ? `我们：${a.content.split("?")[0]}`
            : `${a.type ?? "?"}：${String(a.content ?? "").slice(0, 40)}`),
    }));
  },
  async volume(p) {
    const n = Number(p.get("n"));
    if (!Number.isFinite(n)) throw new Error("缺少 ?n=0-100");
    return { ok: await mina().setVolume(n) };
  },
  async stop() {
    return { ok: await mina().stop() };
  },
  async play(p) {
    const url = p.get("url");
    if (!url) throw new Error("缺少 ?url=");
    if (!url.startsWith(`${BASE}/`)) throw new Error(`只许播本机门的链接（${BASE}）`);
    const ok = await mina().play({ url });
    return { ok, heard: await watch(), url: url.split("?")[0] };
  },
  async song(p) {
    const q = p.get("q");
    if (!q) throw new Error("缺少 ?q=");
    const { url, title } = await song(q);
    const ok = await mina().play({ url });
    // 日志里绝不落完整链接：以前就是这里把主 key 带进过 /tmp/ask.log
    console.log(`🎵 ${title} → ${url.split("?")[0]}`);
    return { ok, title, heard: await watch() };
  },
  // 手动放一条抖音链接到电视（就是本机 gst 那条腿，和问答走的同一套）
  async tv(p) {
    if (p.get("do") === "stop") return (await import("./config.js")).stopScreen();
    const url = p.get("url");
    if (!/douyin\.com\/share\/video\/\d+/.test(url || "")) throw new Error("缺少 ?url=<iesdouyin share/video>");
    const meta = await (await import("./config.js")).showOnScreen(url);
    return meta ? { ok: true, duration: meta.duration } : { ok: false, why: "解不出直链（多半是图文 note）" };
  },
};

createServer(async (req, res) => {
  const u = new URL(req.url, "http://x");
  const name = u.pathname.replace(/^\//, "") || "ask";
  const send = (code, obj) => res.writeHead(code, { "content-type": "application/json; charset=utf-8" })
    .end(JSON.stringify(obj, null, 1) + "\n");
  if (!routes[name]) return send(404, { error: `未知口 /${name}`, 可用: Object.keys(routes) });
  try {
    send(200, await routes[name](u.searchParams));
  } catch (e) {
    send(500, { error: String(e?.message || e) });
  }
}).listen(7799, "127.0.0.1", () =>
  console.log('音箱控制口 http://127.0.0.1:7799/{ask,status,messages,volume,stop,play,song,tv}'));

