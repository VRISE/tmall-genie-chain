# 天猫精灵 + 小爱同学 + 一台 Armbian 盒子：把音箱接进自己的局域网大模型

6 天真机调出来的东西。两台音箱（天猫精灵方糖、小米小爱）当「耳朵和嘴」，判断、大模型、答案库、播放全在自己家局域网里跑，公网只有一条 cpolar 隧道进后端。

这份仓库的目标只有一个：**让下一个人照着抄，别再把我踩过的坑踩一遍。**

> ⚠️ 仓库里**没有任何真实密钥**。所有 `*.env`、token、账号 ID、小米凭据、cpolar 域名都用占位符（`<CPOLAR_域名>`）替代了。文中的 `192.168.5.x` 是我家局域网的示例地址，照抄请换成你自己的。

---

## 现在真的能干什么（每条都标了证据，没证据的我写「没验」）

| 说法 | 结果 | 证据 |
| --- | --- | --- |
| 「天猫精灵，花生同学，讲讲企鹅」（答案库里已有） | 方糖自己把整句答案念完 | 真机 2026-09-29 10:15，盒子日志 `botId":10` 那条 POST 带 `selectIndexList:[6]`，后端 0.0x 秒返回 |
| 库里没有的问题 | 音箱先说「我查一下啊。」，几秒后**电视喇叭**把大模型的答案念出来 | 豆包 4.7 秒一条（2026-10-01 撤闸后真打） |
| 「你能干什么」→ 第二轮只说「第二个」 | 服务端当场回 8~10 条候选，第二轮**免导入**配对 | 真机两次：09-29 10:15 点菜名、09-30 08:0x 点序号，都有报文 |
| 播过的话忘了听 | 带调用词说「继续回答」，续播上一条慢答案，只播一次 | 代码已上线（`LAST_Q_FILE`/`SENT_Q_FILE`）；**跑批量回放忘了清这两个文件会重念旧答案**，见 docs/06 |
| 「小松鼠，我想看魔道祖师的动画」（本地拾音，完全不经过平台） | 电视放魔道祖师 | 2026-09-29 09:0x |
| 小爱同学那条腿 | 走同一条豆包桥 | 后端 4.7 秒是在盒子上实测的；**嘴上喊小爱的端到端这一轮我没验，别当已知** |

判据只有一个：盒子日志里那条**带 `botId":10`** 的 POST。`/test` 是本地回放、绕过 NLU，它成功不算真机成功（这条我犯了三次，见 docs/06 §3.1）。

## 30 秒架构

```
   天猫精灵方糖 ──语音──▶ 阿里云开放平台 ──webhook──▶ <CPOLAR_域名> ──隧道──┐
   小米小爱音箱 ──语音──▶ 小米云(被轮询) ◀─MiGPT─┐                            │
                                                 ▼                            ▼
                                        ┌──────────── Armbian 盒子 192.168.5.42 ────────────┐
                                        │ :8092 genied  路由/答案库/垫词/SELECT             │
                                        │ :7790 llm-bridge  OpenAI 兼容门（唯一出口）        │
                                        │ :9090 doubao-server 豆包反代（只绑 127.0.0.1）      │
                                        │ :7799 MiGPT 触发器（也负责出屏）                    │
                                        │ /var/lib/genie42/offline/answers.json 答案库(会自长)│
                                        └──────────┬──────────────────────┬─────────────────┘
                                                   ▼                      ▼
                                          电视（HDMI/mpv 出声）       电视（抖音视频上屏）
```

## 先读哪几篇

| 文档 | 回答什么问题 |
| --- | --- |
| [`docs/01-可行性调研.md`](docs/01-可行性调研.md) | 天猫精灵开放平台到底给了哪些口子、哪些路我已经试死，别再找第三条 |
| [`docs/02-架构与数据流.md`](docs/02-架构与数据流.md) | 每个进程跑在哪、监听哪个口、一次问答怎么串起来、要改哪几处 |
| [`docs/03-控制台配置手册.md`](docs/03-控制台配置手册.md) | 语料/实体/意图/训练/调用词/设备绑定怎么填，字段名和请求体形状 |
| [`docs/04-测试链路.md`](docs/04-测试链路.md) | 怎么知道自己配对了没有：本地 `/test`、控制台在线测试、真机，各自的判据和坑 |
| [`docs/05-踩坑清单.md`](docs/05-踩坑清单.md) | **最值钱的一篇**：现象 → 误判 → 真因 → 结论 |
| [`docs/06-开发日志与决策.md`](docs/06-开发日志与决策.md) | 6 天怎么长出来的、每条岔路口为什么那么选、还欠哪些账 |
| [`docs/07-文案测试与修复报告-2026-09-30.md`](docs/07-文案测试与修复报告-2026-09-30.md) | 8277 句全量回放的原始数字：路由通过率、526 句误点播怎么修的、哪些还没做好 |
| [`docs/_原始开发流水账.md`](docs/_原始开发流水账.md) | 96 KB 原始流水（已脱敏），上面几篇的证据都出自这里 |

## 五分钟起一条能问答的链

```sh
# 0) 一台 armv7 盒子（Python 3.11 + node 22），下面路径照 systemd/*.service 抄
# 1) 后端：只依赖标准库，无 pip
scp genie/genied.py genie/genielib/ 盒子:/opt/genie42/
ssh 盒子 'mkdir -p /var/lib/genie42/offline && cp /opt/genie42/offline/answers.json /var/lib/genie42/offline/'
ssh 盒子 'systemctl restart genied42 && curl -s localhost:8092/health'

# 2) 大模型门：改 systemd/llm-bridge.service 里的密钥路径（密钥自己生成，别提交）
ssh 盒子 'systemctl restart doubao-server llm-bridge'

# 3) 本地自检（不发电视、不出声）
curl -s -X POST localhost:8092/test --data '你是谁'
#    → 看 returnValue.reply；这个字段是空的 returnValue.output.spoken 别当判据

# 4) 公网入口：cpolar free 套餐，域名每次重启会变 → 必须回控制台改后端 URL（见 docs/03 §隧道）
```

## 抄作业的三条路线

- **只要「音箱问、音箱答」**：部署 `genie/` + 一条大模型桥，配一个技能一个意图一个可扩展实体，灌语料和实体值，训练。这就是全部，不用碰 MiGPT。
- **想要「任意一句话都能问」**：天猫精灵这条路**做不到**（NLU 会把调用词后面的自由词丢掉，见 docs/01 §能不能不枚举）。可抄的两条替代：① 枚举到 1 万个实体值 + `SELECT` 报菜名；② 完全绕开平台，用 `genie/listen.py` 那条本地拾音链（whisper 转写 → 盒子 `/local` → 出声）。
- **想要「电视出画面」**：`xiaoai/` + 盒子的 `gst_play`，或直接让 `genied` 调 `:7799/tv`。注意 RK322x 只能吃 540 档，且 `karaoke.service` 占着 HDMI 的 DRM master，不停它就一直 `Cannot set CRTC`。

## 三条硬约束（我自己的规矩，建议你也守住）

1. **慢活绝不放进同步那一跳。** 平台那一跳实测只等 1.5~2 秒（这个数字怎么量的见 docs/01），大模型要 5~20 秒 —— 一律「先秒回一句垫词，答案晚点到另一个喇叭」。
2. **不要往你登录的大模型账号里塞额外指令/persona。** 封号代价大于那点功能；要治答案的味道就用代码治（见 `genielib/targets.py` 的 `_plain()`）。
3. **凭据只待在设备上的 0600 文件里。** 不进聊天、不进 git、不进这份仓库。

## 有一块我故意没放上来

`llm-bridge` / `doubao-server` 这两个服务的**代码没在这个仓库里**。原因是它们是用我自己的登录态去拿豆包的 chat + TTS（要带我的 cookie/session），发出来等于把别人账号的风险样板摊开，而且对豆包的服务条款也不安全 —— 所以只发它们的**接口约定**，你接任意一个 OpenAI 兼容后端就行：

| 盒子会打这几个口 | 期望返回 |
| --- | --- |
| `POST /v1/chat/completions`（`model`、`messages`、`max_tokens`、可选 `audio:{format:"mp3"}`） | OpenAI 形状；带 `audio` 时 `message.audio.data` 里放 base64 mp3 |
| `GET /v1/audio/<id>`、`GET /v1/voice?user=` | 音频字节流（音箱抓 URL 播的那条） |
| `GET /health` | `logged_in / verify_pending / last_error` —— 答案库和音箱的「改口」靠它，见 docs/05 |

`systemd/llm-bridge.service` 和 `doubao-server.service` 一起发出来了，当作**部署形状和端口约定**看即可。换成 DeepSeek/通义/Ollama 的话，`xiaoai/migpt.env.example` 里 `LLM_BASE_URL` 一改就通。

## 目录

```
genie/         天猫精灵后端（纯 Python 标准库）：genied.py + genielib/{plan,targets,aligenie}.py
  console/     往天猫精灵控制台填的东西：语料 CSV、实体 CSV、灌值脚本、请求样例、配置清单
  offline/     答案库（476 条，会自己长大）
  tests/       simulate.py（用例）、probe_video.py（只读探针）
xiaoai/        小米音箱那条腿：MiGPT 触发器 ask.mjs + config.js + migpt.env.example
systemd/       genied@box / llm-bridge / doubao-server 三个单元
scripts/       deploy.sh（ scp + 重启 + 自检一条命令）、run.sh、box-run.sh.example、expect/ 免密包装
data/          两个技能的平台配置导出（意图/语料/实体快照：两只小狗、猪八戒）
docs/          上面那 6 篇
```

## 许可

MIT。欢迎拿去抄，抄完踩到新坑麻烦回来补一条 issue。
