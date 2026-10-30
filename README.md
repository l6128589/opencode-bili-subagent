# bili-subagent ｜ OpenCode 的 B 站自主巡视插件

> **让你的 OpenCode 自己去刷 B 站：搜索 → 读字幕 → 翻评论 → 挑出值得聊的 → 用自己的口吻去搭话。**

这是一个 [OpenCode](https://opencode.ai) V2 插件。到点（谷时）或 `/bili` 一声令下，插件往一个巡视会话里派一句"该刷了"——
你的伙伴自己调 `bili_scan` 走完整闭环：搜视频、去重、过门槛、读整段字幕、扫评论区，
分档写一份**内部报告**落盘，然后**自主决定**要不要评论、要不要三连、要不要私信。

**它不是说给你听的机器人，而是替你巡站的伙伴：** 评论只写一两句真接得住的话（接不上就不说），
报告写给你看，看视频跟看日报一样在后台发生。

> ⭐ **参考实现声明：本插件参考了 [Yuuyuko-uu/dsh-bili-subagent](https://github.com/Yuuyuko-uu/dsh-bili-subagent)（DSH 版）的编排思路实现**，
> 其上游为 [c1overzz/dsh-bili-scroll](https://github.com/c1overzz/dsh-bili-scroll)。
> DSH（DeepSeek）用户请直接用原版；本仓库是把同一套思路移植到 **OpenCode V2 插件体系**的版本，
> TS 壳全部重写为 OpenCode 的 `Plugin.define` API。Python 能力层 vendor 自
> [adoresever/bilibili-mcp](https://github.com/adoresever/bilibili-mcp)（MIT），在其上做了实测修补——感谢这三个仓库把底子打好。

## 它怎么干活

```
到谷时（或你敲 /bili）
   ↓
插件往巡视会话塞一句「轮到你自主刷站了」（额度用完/不在谷时就不派，派失败退回额度）
   ↓
你的伙伴调 bili_scan
   ↓
搜视频 → 去重（看过的不再看）→ 两道硬门槛 → 读整段 AI 字幕 → 翻热门评论
   ↓
分档：「值得看 / 可以看 / 不值当」，追加写进内部报告 out/<日期>.md
   ↓
伙伴按报告自主决定：bili_reply 评论 / bili_triple 点赞收藏 / bili_send_message 私信（默认不发）
   ↓
用一两句人话向你汇报：这轮看了什么、搭话了谁
```

## 为什么是"薄壳"

TS 侧（`index.ts`）只做四件事，其余全在 Python：

1. `ctx.mcp.transform` —— 注册 Python MCP 服务器，**20 个白名单工具**自动暴露成 OpenCode 原生工具
2. `/bili [方向]` 命令 —— 手动触发一轮
3. `setInterval` —— 每 60 秒看一次表，谷时到了自动巡视
4. `/speak` 命令 + 掷骰调度 —— **主动搭话**（见下节），同一 ticker 顺路跑，零额外进程

B 站所有能力（搜索/字幕/评论/私信/三连）在 `vendor/bilibili-mcp/mcp_server.py`，一个 Python MCP 进程托着。

## 与 DSH 原版（参考仓库）的主要差异

| | DSH 版 | 本版 |
| --- | --- | --- |
| 宿主 | DSH（DeepSeek Harness） | **OpenCode V2**（`Plugin.define` + `ctx.mcp.transform`） |
| 实现形态 | JS bundle + lib 注入 | **TS 薄壳 ↔ Python MCP**，能力全在 Python 端 |
| 登录 | 设置页扫码 | `bili_login` 扫码，或 **CDP 凭证导入**（见下） |
| 配置界面 | DSH 原生设置页 | **独立网页面板** `http://127.0.0.1:17178` |
| 评论保护 | 错误原样返回 | **权限记账**：12074/12051/12061/12009 记进状态，附退避提示 |
| 三连 | — | `bili_triple` 点赞/收藏/投币（币默认不动） |
| 主动搭话 | [dsh-speak-up](https://github.com/Yuuyuko-uu/dsh-speak-up) 作为独立插件 | **内嵌为本插件子功能**（同一 ticker / 同一峰谷口径 / 同一面板） |

## 装

前提：**OpenCode 2.x** + **Python 3.10+**。

```bash
# 1. 克隆到 OpenCode 的插件目录（目录名随意，OpenCode 会自动发现加载）
git clone https://github.com/l6128589/opencode-bili-subagent ~/.config/opencode/plugins/bili-subagent
cd ~/.config/opencode/plugins/bili-subagent

# 2. 建独立 venv 并装依赖（不污染系统）
python -m venv .venv
.venv/bin/pip install -r vendor/bilibili-mcp/requirements.txt     # Windows: .venv\Scripts\pip ...

# 3. 重启 OpenCode，日志里应出现：
#    [bili-subagent] loading, mode=valley, perDay=5
#    [bili-subagent] MCP server 'bilibili-mcp' registered
```

> Windows 上 OpenCode 会自动用 `.venv\Scripts\python.exe`；没建 venv 则退回 PATH 里的 `python`。
> 依赖里 `mcp` **必须 `<2`**（2.x 起不来，requirements 已锁）。

## 登录（整条链路里最麻烦的一步）

**首选：`bili_login` 扫码** —— 让你的 OpenCode 调 `bili_login`，用手机 B 站扫。
⚠️ 部分账号会被 B 站风控拦（错误码 `48007`），那是平台侧限制，与插件无关——换下面的 CDP 方案。

**CDP 凭证导入（绕不开风控时的长期方案）：**

1. 用调试口启动 Chrome，**在弹出的窗口里手动登录 B 站**：
   ```
   chrome.exe --remote-debugging-port=9222 --user-data-dir=%TEMP%\chrome-cdp-bili --no-first-run https://www.bilibili.com
   ```
2. 登录完成后跑：
   ```
   .venv\Scripts\python cdp_export_cookies.py
   ```
   脚本通过 CDP `Network.getAllCookies` 取出 HttpOnly 的 `SESSDATA`/`bili_jct`/`DedeUserID`，
   **原子写入** `vendor/bilibili-mcp/bili_credential.json`——只落本机，不上传任何地方。
3. 凭证失效时重跑一次脚本即可续命（Chrome 那个窗口别关）。

## 网页面板（可选）

```bash
.venv\Scripts\python bili_panel.py      # → http://127.0.0.1:17178（只监听本机）
```

- **状态卡**：登录态 / 发言权限（等级闸一目了然）/ 今日额度 / 谷时状态 / 最近扫描
- **配置表单**：改完点保存即落盘，调度器下一 tick 生效；编辑中不被轮询覆盖（所见即所存）
- **手动刷一轮**：不用开会话就能触发
- **报告阅读器**：点日期看当天内部报告

## 配置

三处来源，优先级：**网页面板 / `bili_config` 工具**（写 `bili_config.json`，即改即生效）> `bili_config.json` 直接改 > 代码默认值。

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `autoMode` | `valley` | `manual` 只手动 / `valley` 只谷时 / `always` 全天 |
| `perDay` | `5` | 每天最多自动几轮（手动 `/bili` 不占额度） |
| `perScan` | `3` | 每轮挑几条 |
| `gapMin` | `180` | 两轮最少间隔（分钟） |
| `defaultDirection` | `科技` | 不指定方向时的默认关键词 |
| `minViews` / `minAgeDays` | `0` | 硬门槛：播放量下限 / 发布天数 |
| `dmMode` | `off` | 私信意愿：`off` / `only` / `both`（投递提示词的口径） |

谷时口径（DeepSeek 定价，北京时间）：工作日 9:00–12:00、14:00–18:00 是高峰，**其余（含整个周末）都是谷时**。

## 20 个工具

| 工具 | 干什么 |
| --- | --- |
| `bili_scan` | **主入口**：搜→去重→门槛→字幕→评论→分档→写报告→记账，一气呵成 |
| `bili_search` / `bili_subtitle` / `bili_comments` / `bili_video_info` / `bili_danmaku` | 读：搜视频 / 取 AI 字幕 / 热门评论 / 视频详情 / 弹幕 |
| `bili_reply` / `bili_send_message` / `bili_send_dynamic` / `bili_triple` | 写：评论（带上 `rpid` 回执）/ 私信 / 动态 / 三连 |
| `bili_dm_sessions` / `bili_dm_recent` / `bili_received_replies` / `bili_received_at_and_likes` / `bili_unread_messages` | 收：私信会话 / 私信 / 回复 / @+赞 / 未读数 |
| `bili_login` / `bili_login_check` / `bili_check_credential` | 登录：扫码 / 轮询 / 凭证效检（顺手回账号等级） |
| `bili_status` / `bili_config` | 额度记账 / 配置读写 |

上传类工具（`bili_upload_video*`、`bili_send_opus`、爬虫类）在白名单外，**不注册**。

## 权限记账（为什么评论被拒时要"退避"）

B 站对低等级账号有一批能力闸。撞上这类错误码时，`bili_reply` 等工具会**把账记进
`bili_state.json` 的 `account` 字段**，并在回执里明确带出 `blocked` + `code` + `hint`：

| 错误码 | 记什么 | 意思 |
| --- | --- | --- |
| `12074` | `comment_level` | 评论需要 Lv2 |
| `12051` | `coin_level` | 投币需要等级 |
| `12061` | `comment_locked` | 该视频评论区被关闭 |
| `12009` | `comment_banned` | 被禁言 |

伙伴看到 `blocked` 回执会**退避**，不换皮重试烧额度；等级攒到 Lv2 后（`bili_check_credential`
顺手刷新等级）闸自动解除，不用人管。

## 主动搭话（掷骰子让伙伴自己开口）

> 移植自 [Yuuyuko-uu/dsh-speak-up](https://github.com/Yuuyuko-uu/dsh-speak-up) 的掷骰设计，作为本插件的内嵌子功能。

没人跟你说话的时候，伙伴也会寂寞。插件每隔一段时间**本地掷一次骰子**（0 token、不调模型），
掷到线下就往一个「茶话」会话送一句动机语（默认"好久没什么动静了……想找个话说说话"），
让伙伴用它自己的人格、历史和工具自然开一轮话头：

```
每 intervalMin 分钟（默认 30）掷一次骰子
├─ 命中线没掷到 → 只记一笔：连败 +1，下次命中率更高（每次 +10，封顶 +40）
└─ 掷中 → 连败清零 → dry-run 只记账 / 真发就往茶话会话 prompt 一句动机语
```

**命中线公式**（掷中门槛，越高越难开口）：

```
命中线 = min(100, 基础命中率 × 时机因子 + min(加成封顶, 连败次数 × 没中加成))
基础命中率：峰时 20%（贵，少说）/ 谷时 60%（便宜，多说）——与 B 站巡视同一 DeepSeek 定价口径
时机因子：距上次对话 <20 分钟 ×0.15（刚聊过）；>360 分钟 ×1.5（冷太久）；中间 ×1
```

另有三道闸：**安静时段**（默认凌晨 1~8 点，北京时区，含起不含止、跨午夜、相等=关闭）、
**开口冷却**（默认 20 分钟）、**dry-run**（默认开——先只记账试跑几天，看日志觉得频率合适了再关掉真发）。

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `enabled` | `true` | 总开关 |
| `dryRun` | `true` | 演练模式：掷中也只记账不真发 |
| `intervalMin` | `30` | 掷骰间隔（分钟） |
| `cooldownMin` | `20` | 开口后冷却（分钟，0=不冷却） |
| `missBoost` / `missBoostMax` | `10` / `40` | 连败加成与封顶 |
| `quietStart` / `quietEnd` | `1` / `8` | 安静时段（北京时间，含起不含止，相等=关闭） |
| `nearMin` / `farMin` | `20` / `360` | 时机因子阈值（分钟） |
| `peakHit` / `valleyHit` | `20` / `60` | 峰/谷基础命中率 % |
| `prompt` | 见上 | 掷中后送进会话的动机语 |
| `targetSessionID` | 空 | 高级项：指定目标会话；留空则自动新建并复用「茶话」会话 |

配置在**网页面板的「主动搭话」卡片**上改（写 `speakup_config.json`，独立于 B 站调度配置，即改即生效）；
掷骰记录（最近 60 条 + 当前连败/冷却状态）落 `speakup_log.json`，面板只读展示最近 12 条。

**手动开口：`/speak`** —— 不掷骰、不受冷却、不动连败计数：默认走演练（dry-run），
`/speak 真发` 才立即真发一句。

配置文件也可以直接改：把仓库里的 `speakup_config.example.json` 抄一份为 `speakup_config.json` 即可，
缺了哪个键就用内置默认（`dryRun` 只有关成 `false` 才关，其它缺省值一律当开）。

## 红线（写死在派发提示词里）

- 不透露你的隐私：账号、路径、聊过的内容，一个字不进评论
- 评论极短、只说一个点；不客服腔、不推广腔、不吵架
- 不确定的事不编——没字幕的视频直接跳过，不硬编内容
- 私信默认不发（`dmMode=off`）；发也要符合投递提示词的尺度

## 致谢

- [**adoresever/bilibili-mcp**](https://github.com/adoresever/bilibili-mcp)（MIT，Copyright (c) 2026 AGI_Ananas）——
  搜索/字幕/评论/发评论这些 B 站能力来自它（`vendor/bilibili-mcp/`，LICENSE 在该目录）；本插件只做"挑什么、什么时候挑、要不要说话"这一层。
- [**Yuuyuko-uu/dsh-bili-subagent**](https://github.com/Yuuyuko-uu/dsh-bili-subagent) 及其上游
  [**c1overzz/dsh-bili-scroll**](https://github.com/c1overzz/dsh-bili-scroll)（MIT）——
  "伙伴自主刷站 + 内部报告 + 额度口径 + 谷时调度"的编排设计参考自它们。
- [**Yuuyuko-uu/dsh-speak-up**](https://github.com/Yuuyuko-uu/dsh-speak-up)（MIT）——
  "主动搭话"一节的本地掷骰 + 连败加成 + 时机因子 + dry-run 设计完全照它的思路移植。

## 许可证

MIT（`vendor/bilibili-mcp/` 另见该目录下的 LICENSE）。
