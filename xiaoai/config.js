// Secrets and addresses live in ./migpt.env, never in this file.
import { createHmac } from 'node:crypto';
import { execFile } from 'node:child_process';
import { appendFileSync, readFileSync, writeFileSync } from 'node:fs';

const k = (name, fallback = '') =>
  (process.env[name] ?? '').trim() || fallback;

const FLY_URL = k('FLY_BRAIN_URL');

// 豆包的声音：反代会把刚答完的那句合成 mp3 存着，答完来 /v1/voice 问地址。
const VOICE_BASE = (k('VOICE_BASE_URL') || k('LLM_BASE_URL'))
  .replace(/\/v1\/?$/, '').replace(/\/+$/, '');
const DOUBAO_VOICE = k('DOUBAO_VOICE') === '1' && !!VOICE_BASE;

async function doubaoVoice() {
  try {
    const res = await fetch(`${VOICE_BASE}/v1/voice`, {
      headers: { authorization: `Bearer ${k('LLM_API_KEY')}` },
      signal: AbortSignal.timeout(4000),
    });
    if (!res.ok) return null;
    const meta = await res.json();
    // 小爱自己去抓字节，它发不了 header，凭据只能挂在 URL 上 ——
    // 但绝不用主 key：MiGPT 引擎会把播的 URL 原样打进日志（2026-09-26 就这么漏过一次），
    // 所以挂"只认这一条音频"的派生 token（llm_bridge 的 ?t=，从主 key + 路径 HMAC 出来）。
    const tok = createHmac("sha256", k("LLM_API_KEY"))
      .update(meta.url).digest("hex").slice(0, 32);
    return `${VOICE_BASE}${meta.url}?t=${tok}`;
  } catch {
    return null;
  }
}

// —— 播放那条腿：这台盒子自己 HDMI 出屏（rkvdec 硬解 + kmssink + alsasink plughw:0,0），
// 不再经过 adb 和那台安卓盒子。小爱上问的、豆包给了视频的，就在这台盒子接的电视上放。
// 全部异步跑，不挡音箱那份答案。
const PLAYER = {
  on: k('PLAYER_ON', '1') === '1' && !!VOICE_BASE,
  py: k('PLAYER_PY', '/root/fly/gst_play.py'),
  bin: k('PLAYER_PYTHON', '/usr/bin/python3'),
  // 这台 RK322x 只能吃 540 档：1080 档连硬解都追不上（实测整片 352 秒跑 8 分钟没完）
  maxH: k('PLAYER_MAX_H', '540'),
  // 开机自启的 karaoke.service 占着 HDMI 的 DRM master，不停它就 `Cannot set CRTC: Permission denied`
  grab: k('PLAYER_GRAB', 'karaoke.service'),
  // 每次真上屏都往这儿追加一行 JSON，`his` 那个列表程序读它（只存标题/链接/档位，不存带 token 的直链）
  hist: k('PLAY_HISTORY', '/root/fly/play-history.jsonl'),
};

// 豆包给的抖音链接就这么几种骨架，一次全认下来：
//   iesdouyin.com/share/video/<id> · douyin.com/video/<id>（裸串也认，absUrl 补 https）
// 刻意不收 `/note/`、`/slides/` —— 那是图文，页面根本没有 `<video>`，拉播放器只会留一个黑屏。
// `v.douyin.com/xxxx` 短链要先 302 才拿得到 id，目前实测没见过豆包发这种；服务端每轮会把
// 看到的链接**形状**打进日志（[links.seen]），真出现了再加展开。
const VSRC = '(?:https?://)?[a-z0-9.-]*douyin\\.com/(?:share/)?video/\\d+';
const VIDEO_CARD = new RegExp(VSRC, 'gi');
// .test() 用在带 /g 的正则上是"有状态"的（lastIndex 每次往后爬），判断单条必须另开一个不带 g 的
const IS_VIDEO = new RegExp('^(?:' + VSRC + ')$', 'i');

// 整句除了链接没别的内容 —— 这种轮次播语音等于让小爱把 "https 冒号 斜杠 斜杠 w w w 点"
// 一个个字母念完（用户 2026-10-01 点名：无意义）。markdown 壳、标点、空白都扒掉再看还剩字没有。
const onlyLink = (s) => {
  if (!s) return false;
  // VSRC 那条也过一遍：豆包正文里的抖音链接常是裸串（没有 https://）
  const left = s.replace(/https?:\/\/\S+/gi, '').replace(new RegExp(VSRC, 'gi'), '')
    .replace(/[\s\p{P}\p{S}]/gu, '');
  return !left;
};

// 链接形状（数字换成 <id> 只看骨架）—— 日志打这个，漏了哪种形式一眼看出来
const shapes = (urls) => [...new Set((urls || []).map((u) => String(u)
  .replace(/^https?:\/\//, '').replace(/\d{6,}/g, '<id>')))].sort();

// 豆包写进正文的链接常常是**裸串**（`iesdouyin.com/share/video/123`，没有 https://）。
// /v1/media 里 urljoin 会兜住，但 /v1/play 开头就查 `http(s)://` 白名单 —— 裸串直接被门
// 回 400 Bad Request，播放器拿到 0 字节，表现就是"音箱说完电视什么都不播"。
// 卡片里那份是完整 URL，所以只有走卡片的那几次能放出来。
const absUrl = (u) => (/^https?:\/\//.test(u) ? u : `https://${u}`);

// 和音箱那条同一种形式：token 在路径里，query 只剩一个参数，整条 URL 没有 &
const playUrl = (resource) => {
  const tok = createHmac('sha256', k('LLM_API_KEY'))
    .update(resource).digest('hex').slice(0, 32);
  const [path, query] = resource.split('?');
  return `${VOICE_BASE}${path}/t/${tok}?${query}`;
};

const run = (cmd, args, timeoutMs = 120000) => new Promise((resolve) => {
  execFile(cmd, args, { timeout: timeoutMs }, (e, out) => resolve(e ? null : out));
});

let playing = null;   // 当前那个 gst_play 子进程，新的来了先掐旧的
let playerGone = Promise.resolve();  // 它**真退出**（含 gst 收尾）的那一刻
let queued = Promise.resolve();     // 屏幕和声卡都只有一份，两次投放必须排队

// 连说两句时以前必踩的坑：新的 gst 起来时上一条还在 set_state(NULL) 收尾，
// DRM master / 声卡没交出来，kmssink 就报 "Cannot set CRTC: Permission denied"，
// 表现是"音箱说了没反应"。所以整个上屏动作串成一条队。
export function showOnScreen(share, question = '') {
  const job = queued.then(() => playOnBox(share, question));
  queued = job.catch(() => null);
  return job;
}

// 音箱那边喊"停掉电视"走的是我们这条腿（小爱自己的暂停/停止只管它自己那份音频，管不到 HDMI）
export async function stopScreen() {
  if (!playing) return { playing: false };
  playing.kill('SIGTERM');
  await playerGone;   // 含 gst 收尾 + 把 karaoke.service 还回去
  return { playing: false, stopped: true };
}

// 播过的东西记一笔，给他那条 `his` / karaoke 的「视频历史」屏用。
// **标题用他那一刻问的原话**（"我要在电视上看晚霞"），不是抖音那串标题 ——
// 他记的是自己问了什么，不是上传者写了什么。没有问句（从列表里重播）就沿用同一条
// 旧记录上的问句，问句也没有才退回视频标题。
// 只记豆包给的原始 share 链接（重播时现解，直链几小时就过期，而且带 ?t=）。落盘失败不能影响播放。
function noteHistory(share, meta, secs, question = '') {
  try {
    const id = share.match(/video\/(\d+)/)?.[1] || '';
    let q = String(question || '').replace(/\s+/g, ' ').trim();
    let title = String(meta.title || '').replace(/\s+/g, ' ').trim();
    if (!q) {
      // 倒着找同一条视频最近一次记的问句（重播时不该把它丢了）
      const lines = readFileSync(PLAYER.hist, 'utf8').split('\n').reverse();
      for (const line of lines) {
        try {
          const r = JSON.parse(line);
          if (r.id === id && r.q) { q = r.q; break; }
        } catch { /* 半行/空行跳过 */ }
      }
    }
    appendFileSync(PLAYER.hist, `${JSON.stringify({
      at: new Date().toISOString().slice(0, 16).replace('T', ' '),
      id, q, title: title.slice(0, 60), url: share,
      gear: meta.gear || '', h: meta.height || 0, secs,
    })}\n`, { mode: 0o600 });
  } catch (e) { console.warn('历史记录写不进去:', e.message); }
}

async function playOnBox(share, question = '') {
  const enc = (s) => encodeURIComponent(s).replace(/[!'()*]/g,
    (c) => `%${c.charCodeAt(0).toString(16).toUpperCase()}`);  // 得和 Python quote(safe='') 逐字节一样
  const resource = `/v1/play?url=${enc(share)}&max_height=${PLAYER.maxH}`;
  const auth = { authorization: `Bearer ${k('LLM_API_KEY')}` };
  // 先确认解得开再上屏：豆包给的卡片里混着图文 note，那种永远等不到 <video>，
  // 直接把播放器拉起来只会留一个黑屏。顺带这一步也把直链捂热了，播放器第一次 GET 才能秒开。
  const meta = await fetch(`${VOICE_BASE}/v1/media?url=${enc(share)}&max_height=${PLAYER.maxH}`,
    { headers: auth, signal: AbortSignal.timeout(120000) })
    .then((r) => (r.ok ? r.json() : null)).catch(() => null);
  if (!meta?.src) return null;

  // 先掐旧的并等它把 DRM/声卡交出来，再动 karaoke —— 反过来的话两条管线抢同一块屏幕
  playing?.kill('SIGTERM');
  await playerGone;
  const wasKaraoke = (await run('systemctl', ['is-active', PLAYER.grab], 5000))?.trim() === 'active';
  if (wasKaraoke) await run('systemctl', ['stop', PLAYER.grab], 20000);
  const secs = Math.min(600, Math.round(Number(meta.duration) || 30) + 2);
  // 日志只写 aweme id，绝不写那条带 ?t= 的完整地址（引擎会把播的 URL 原样打进日志）
  console.log(`📺 本机出屏 ${share.match(/\d+$/)?.[0] || ''} ${secs}s`);
  // gst 的报错里常带完整 URI（含 ?t=），落日志和落盘前都得抹掉
  const mask = (s) => String(s).replace(/\/t\/[0-9a-f]{32}/g, '/t/<tok>')
    .replace(/url=[^&\s"]+/g, 'url=<enc>');
  noteHistory(share, meta, secs, question);
  playerGone = new Promise((done) => {
    playing = execFile(PLAYER.bin, [PLAYER.py, playUrl(resource), String(secs)],
      { timeout: (secs + 45) * 1000, env: { ...process.env, ADEV: k('PLAYER_ADEV', 'plughw:0,0') } },
      (err, out, stderr) => {
        playing = null;
        if (err) {
          // 真正的失败原因 gst_play 是打在 **stdout** 上的（"报错: …"），只看 stderr 会一无所获
          const all = mask(`${out || ''}\n${stderr || ''}`);
          console.warn('播放器没起来:', all.split('\n').filter(Boolean).slice(-2).join(' | ')
            || mask(err.message).slice(-200));
          writeFileSync('/tmp/player.err', all || mask(String(err?.message || err)), { mode: 0o600 });
        }
        done();
      });
  }).then(() => {
    // 只有本来就在跑的才还回去 —— 人手动停着腾屏幕的话别给启动回来
    if (wasKaraoke) return run('systemctl', ['start', PLAYER.grab], 20000);
  });
  return meta;
}

async function tvFromDoubao(replyText = '', question = '') {
  if (!PLAYER.on) return null;
  try {
    const res = await fetch(`${VOICE_BASE}/v1/cards`, {
      headers: { authorization: `Bearer ${k('LLM_API_KEY')}` },
      signal: AbortSignal.timeout(6000),
    });
    const cards = res.ok ? ((await res.json()).cards || []) : [];
    // 卡片和正文两头都捞：实测豆包经常只在正文里给链接、cards 是空的（2026-09-26 01:40 那次就是）
    const raw = [...cards.map((c) => c.url || ''), ...(replyText.match(VIDEO_CARD) || [])];
    const hits = [...new Set(raw.filter((u) => IS_VIDEO.test(u)).map(absUrl))];
    // 分清来源和形状：`候选0 形状=[]` = 这轮豆包压根没给链接（它不是每轮都联网），
    // `候选N` 有但没播出来 = 解链那步的问题。别靠"播来播去同一条"猜。
    console.log(`📺 候选${hits.length} 卡片[${shapes(cards.map((c) => c.url)).join(',')}]`
      + ` 正文[${shapes(replyText.match(VIDEO_CARD)).join(',')}] http=${res.status}`);
    for (const hit of hits) {
      if (await showOnScreen(hit, question)) return hit;
    }
    if (hits.length) console.warn('电视没播起来：这轮的抖音链接都解不出直链');
    return null;
  } catch (e) {
    console.warn('电视没播起来:', e?.message || e);
    return null;
  }
}


// 不再"追问一句要链接"（2026-09-26 16:0x 撤掉）：那是往豆包里塞指令，账号有风险。
// 豆包这一轮没联网搜索就是不给链接，只能等它下一次自己给。


async function fly(route, body = {}, timeoutMs = Number(k('FLY_BRAIN_TIMEOUT_MS', '9000'))) {
  if (!FLY_URL) return null;
  const ac = new AbortController();
  const timer = setTimeout(() => ac.abort(), timeoutMs);
  try {
    const res = await fetch(`${FLY_URL.replace(/\/$/, '')}/${route}`, {
      method: 'POST',
      signal: ac.signal,
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (!res.ok) return null;
    return await res.json();
  } catch {
    return null;
  } finally {
    clearTimeout(timer);
  }
}

const number = (v) => (typeof v === 'number' ? v.toFixed(1) : '0');

async function flyStatus() {
  if (!FLY_URL) return null;
  try {
    const res = await fetch(`${FLY_URL.replace(/\/$/, '')}/health`, {
      signal: AbortSignal.timeout(4000),
    });
    return res.ok ? await res.json() : null;
  } catch {
    return null;
  }
}

const ALARM = k('ALARM_WORDS', '打,砸,吓,冲,杀,滚,崩,炸').split(/[,，]/).filter(Boolean);

// 这些开头的句子**原样交给小爱自己处理**，我们不接：播歌/调音量/关机/静音是它的原生能力，
// 一接就得先压它的音量再打断它，那条原生指令反而做不成（用户 2026-09-26 16:0x 点名）。
// 词表可扩：migpt.env 里 NATIVE_WORDS=播放,大点声,... 逗号分隔。
const NATIVE = k('NATIVE_WORDS',
  '播放,放歌,来一首,来首,大点声,小点声,大声点,小声点,音量,静音,关机,睡觉,退下,暂停,停止,下一首,上一首,切换,换一首')
  .split(/[,，]/).filter(Boolean);

// 这些词**必须**先过我们这一关：小爱自己的"暂停/停止"只管它自己那份音频，掐不掉 HDMI 上的画面。
// 所以带"电视/屏幕"字样的停下来是我们在停播放器，别的仍按原样交回小爱（见 NATIVE）。
const SCREEN_STOP = /(电视|屏幕).{0,3}(停|关|退|别播|别放|别响)|(停|关)(掉|了)?.{0,3}(电视|屏幕)/;

// 默认**不往豆包发任何 system 提示词**（2026-09-26 16:0x 定）：我们发的是它自家网页端的登录态，
// 每轮额外塞一段"你是个音箱助手…"这种指令最容易被判成脚本用，账号比什么都贵。
// 想要 persona 就在 migpt.env 里设 SYSTEM_PROMPT=...，空着就等于不发（@mi-gpt/chat 只在真值时才加）。
const SYSTEM_PROMPT = k('SYSTEM_PROMPT', '');

// 小爱自己那一份答案（COIN）压不住：它在我们这轮开口之前就已经在说了，小米云也没有
// "只应答、不作答"的开关。不 root 只有静音窗口这一招 —— 检测到这句话立刻把音量拉到 0，
// 我们的音频到嘴边再还回去。只静音还不够：COIN 能说十几秒，恢复音量时它的尾巴会漏出来，
// 所以收尾先 stop 再还音量（打断抄 xiaogpt：player_play_operation {action:"stop"}）。
const muteWindow = async (engine) => {
  const mina = engine?.MiNA;
  if (k('MUTE_COIN', '1') !== '1' || !mina) return async () => {};

  const volume = (v) => mina
    .callUbus('mediaplayer', 'player_set_volume', { volume: v })
    .then((r) => r?.code === 0, () => false);

  const was = await mina.getVolume().catch(() => null);
  // MiNA.setVolume 被上游 clamp 在 6..100（6 在安静房间里还听得见），
  // 所以要 0 就得绕过它直发 ubus。
  let off = (await volume(0)) || (await volume(1));
  if (!off) off = await mina.setVolume(6).catch(() => false);
  if (!off) return async () => {};

  let done = false;
  const restore = async () => {
    if (done) return;
    done = true;
    clearTimeout(guard);
    await mina.stop().catch(() => {});
    if (was) await volume(was).catch(() => {});
  };
  // 兜底：这一路任何一步抛错、豆包卡死，都不能把音箱永久留在静音状态。
  const guard = setTimeout(restore, Number(k('MUTE_GUARD_MS', '45000')));
  guard.unref?.();
  mina.stop().catch(() => {});
  setTimeout(() => mina.stop().catch(() => {}), 800).unref?.();
  return restore;
};

export default {
  debug: k('DEBUG') === '1',
  speaker: {
    did: k('MI_SPEAKER_NAME'),
    userId: k('MI_USER_ID'),
    password: k('MI_PASSWORD'),
    passToken: k('MI_PASS_TOKEN') || undefined,
    heartbeat: Number(k('HEARTBEAT_MS', '1000')),
  },
  openai: {
    baseURL: k('LLM_BASE_URL', 'https://api.deepseek.com/v1'),
    apiKey: k('LLM_API_KEY', 'none'),
    model: k('LLM_MODEL', 'deepseek-chat'),
    enableProxy: false,
    extra: {
      createParams: {
        max_tokens: Number(k('LLM_MAX_TOKENS', '300')),
        temperature: Number(k('LLM_TEMPERATURE', '0.7')),
        // 带上这个字段，门那边才会顺手把答案念成 mp3（它会原样转发给豆包）
        ...(DOUBAO_VOICE ? { audio: {} } : {}),
      },
    },
  },
  prompt: { system: SYSTEM_PROMPT },
  context: { historyMaxLength: Number(k('HISTORY_MAX', '10')) },
  // Left empty on purpose: onMessage below answers every utterance, so the
  // keyword gate would only ever reject what it does not reach.
  callAIKeywords: [],

  async onMessage(engine, msg) {
    const t = (msg.text || '').trim();
    // 先认"停电视"这一类：小爱的暂停掐不到 HDMI 画面，只有我们能杀那个 gst 子进程
    if (SCREEN_STOP.test(t)) {
      const r = await stopScreen();
      console.log(`📴 停电视：${t} → ${JSON.stringify(r)}`);
      return { text: r.stopped ? '电视停了。' : '电视本来就没在放。' };
    }
    // 原生指令：连静音窗口都不开，让它自己的音量/播歌/关机照常走完
    if (NATIVE.some((w) => t.startsWith(w))) {
      console.log(`⏭️ 原生指令，不接管：${t}`);
      return { handled: true };
    }
    const unmute = await muteWindow(engine);
    try {
      return await answer(engine, msg);
    } finally {
      await unmute();
    }
  },
};

async function answer(engine, msg) {
  const text = msg.text.trim();

  if (text.startsWith('果蝇状态') || text.startsWith('苍蝇状态')) {
    const h = await flyStatus();
    return {
      text: h
        ? `果蝇服务器在线。${h.neurons} 个神经元，${h.synapses} 个突触，后端 ${h.backend}。` +
          `开机到现在跑了 ${h.runs} 次仿真，累计 ${Math.round(h.cumulative_spikes)} 个脉冲，` +
          `巨纤维最高放电每秒 ${number(h.gf_peak_hz)} 次。`
        : '果蝇服务器没在。检查那台 Mac 上的 speaker_api 是否还开着。',
    };
  }

  const shouted = ALARM.some((w) => text.includes(w));
  // 脑仿真和大模型并行：1.7 s 的 GPU 推演藏在 LLM 延迟里，不额外加时。
  const brainPromise = shouted ? fly('loom', { text }) : fly('stimulate', { text });
  const ai = await engine.askAI(msg);
  const brain = await brainPromise;

  if (brain && (brain.gf_hz ?? 0) >= Number(k('GF_REFUSE_HZ', '50'))) {
    return {
      text: `等下。刚才那句话说完，这只果蝇脑的巨纤维放电到了每秒${number(brain.gf_hz)}次，` +
        `那是它受惊逃跑时的频率。我不想在一个把苍蝇吓到逃跑的房间里回答问题。`,
    };
  }

  const reply = typeof ai === 'string' ? ai : ai?.text;
  if (!reply) return { text: '我没听清，再说一遍。' };

  // 豆包的声音就是这句 reply，播它就没必要再让小爱念一遍，果蝇前缀也一起省掉
  const linkOnly = onlyLink(reply);
  const voice = DOUBAO_VOICE && !linkOnly ? await doubaoVoice() : null;
  // 这轮豆包顺手给了视频 → 电视那条腿自己去点，不占音箱这份答案的时间
  tvFromDoubao(reply, text);   // 问句一起带上：播放历史拿它当标题
  if (voice) return { url: voice };
  if (linkOnly) {
    // 不播语音 URL（那等于念字母），改说一句人话；视频该上电视的照旧上（上面那行不等它）
    const video = (reply.match(VIDEO_CARD) || []).some((u) => IS_VIDEO.test(u));
    return { text: k(video ? 'SAY_VIDEO_LINK' : 'SAY_PLAIN_LINK',
      video ? '给你投放视频了，在电视上看。' : '豆包就回了条链接，念出来全是字母，不念了。') };
  }

  if (!brain || k('FLY_IN_REPLY') !== '1') return { text: reply };
  return { text: `（这只脑现在每秒${number(brain.mean_hz)}赫兹，${brain.mood}。）${reply}` };
}
