// bili-subagent —— OpenCode V2 薄壳插件
// 职责只有三件，B 站所有能力都在 vendor/bilibili-mcp/mcp_server.py（Python MCP）里：
//   ① ctx.mcp.transform  → 注册 Python MCP 服务器（工具经白名单 BILI_MCP_TOOLS 裁剪，自动暴露成原生工具）
//   ② ctx.command.transform → 注册 /bili 命令：手动触发一轮「搜索→字幕→评论→内部报告」
//   ③ setInterval 谷时调度 → 到点往目标会话塞一句"该刷了"，由会话里的伙伴调 bili_scan 走完整闭环
// 卸载时清掉定时器（返回 cleanup）。

import { existsSync } from "node:fs"
import { copyFile, mkdir, readFile, rename, writeFile } from "node:fs/promises"
import { dirname, join } from "node:path"
import { fileURLToPath } from "node:url"
import { Plugin } from "@opencode/plugin"

// ---------- 路径：插件家目录 + venv Python + MCP 服务器脚本 ----------
// 家目录不写死：跟随本文件所在目录（装到 ~/.config/opencode/plugins/bili-subagent 即用）
const PLUGIN_DIR = dirname(fileURLToPath(import.meta.url))
const SERVER_PY = join(PLUGIN_DIR, "vendor", "bilibili-mcp", "mcp_server.py")
// Python 优先用插件自建的 venv（Windows / Unix 两种布局都认），没有就退回 PATH 里的 python
const IS_WIN = process.platform === "win32"
const VENV_PY = join(PLUGIN_DIR, ".venv", IS_WIN ? "Scripts" : "bin", IS_WIN ? "python.exe" : "python")
const PYTHON_EXE = existsSync(VENV_PY) ? VENV_PY : "python"

// 工具白名单：登录/读（搜索、字幕、评论、私信、通知）+ 写（回复、私信、动态、三连）+ 编排（scan/status/config）。
// 上传类工具（bili_upload_video*、bili_send_opus、bili_crawl、bili_crawl）不放行。
const ALLOWED_TOOLS = [
  "bili_login", "bili_login_check", "bili_check_credential",
  "bili_search", "bili_subtitle", "bili_comments", "bili_video_info", "bili_danmaku",
  "bili_reply", "bili_send_message", "bili_send_dynamic", "bili_triple",
  "bili_dm_sessions", "bili_dm_recent",
  "bili_received_replies", "bili_received_at_and_likes", "bili_unread_messages",
  "bili_scan", "bili_status", "bili_config",
].join(",")

// ---------- 选项：默认值 ← opencode.json 的 ctx.options 覆盖 ----------
type BiliOptions = {
  autoMode: string        // manual（只手动）| valley（只在谷时自动）| always（全天）
  perDay: number
  perScan: number
  gapMin: number          // 两轮最少间隔（分钟）
  defaultDirection: string
  targetSessionID: string // 固定往这个会话派发；空则自建会话并复用
}

function clampNum(value: unknown, fallback: number, min: number, max: number): number {
  const n = Math.floor(Number(value))
  if (Number.isNaN(n)) return fallback
  return Math.min(max, Math.max(min, n))
}

function buildOptions(raw: unknown): BiliOptions {
  const src = (raw !== null && typeof raw === "object" ? raw : {}) as Record<string, unknown>
  const mode = String(src.autoMode || "valley")
  return {
    autoMode: mode === "manual" || mode === "always" ? mode : "valley",
    perDay: clampNum(src.perDay, 5, 0, 100),
    perScan: clampNum(src.perScan, 3, 1, 10),
    gapMin: clampNum(src.gapMin, 180, 5, 720),
    defaultDirection: String(src.defaultDirection || "").trim() || "科技",
    targetSessionID: String(src.targetSessionID || "").trim(),
  }
}

// 谷时判定（DeepSeek 定价口径，北京时间）：工作日 9:00–12:00、14:00–18:00 是高峰，其余（含整个周末）都是谷时
function inValleyBJ(): boolean {
  const bj = new Date(Date.now() + 8 * 3600_000)
  const hour = bj.getUTCHours()
  const day = bj.getUTCDay()
  if (day === 0 || day === 6) return true
  const peak = (hour >= 9 && hour < 12) || (hour >= 14 && hour < 18)
  return !peak
}

// ---------- 派发提示词：插件不说给用户听，只把"该刷了"塞进会话，由伙伴自己走闭环 ----------
function buildScanPrompt(direction: string, perScan: number, reason: string): string {
  return [
    "轮到你自主刷站了（" + reason + "）。按这个流程走：",
    "",
    '1. 调 bili_scan 工具，direction 用 "' + direction + '"（想换方向就自己定），count 上限 ' + perScan + "。",
    "2. 收到内部报告后自己判断：标题、讲了什么、评论区反响都给你了，挑出你真心觉得值得聊的。",
    "3. 认同的话按配置用 bili_reply（评论）/ bili_send_message（私信）/ bili_triple（三连）走后续动作；",
    "   口吻自然、说人话，不要客服腔和推广腔；不认同的话一条都不动，也行。",
    "4. 完成后用一两句人话向我汇报：这轮看了什么、搭话了谁，报告正文已落盘到 out/<日期>.md。",
    "5. 若 bili_scan 提示未登录，先调 bili_login 让我扫码。",
  ].join("\n")
}

type SchedState = {
  date: string        // 北京日期，跨天自动清零
  rounds: number
  lastRunAt: number   // 本轮派发时刻（毫秒）
  sessionID: string   // 自建并复用的巡视会话
}

// ---------- 主动搭话（speak-up，掷骰思路参考 Yuuyuko-uu/dsh-speak-up：本地随机 0 token，掷中才让伙伴开口）----------
type SpeakUpCfg = {
  enabled: boolean
  dryRun: boolean
  intervalMin: number
  cooldownMin: number
  missBoost: number
  missBoostMax: number
  quietStart: number
  quietEnd: number
  nearMin: number
  farMin: number
  peakHit: number
  valleyHit: number
  prompt: string
  targetSessionID: string
}

const SPEAKUP_DEFAULTS: SpeakUpCfg = {
  enabled: true,
  dryRun: true,          // 默认演练：掷中了也只记账不真发，安全试跑
  intervalMin: 30,       // 每隔多少分钟掷一次骰子
  cooldownMin: 20,       // 开口后这么多分钟内不再开口（0 = 不冷却）
  missBoost: 10,         // 每没中一次，下次命中线加这么多百分点
  missBoostMax: 40,      // 加成封顶
  quietStart: 1,         // 安静时段起点小时（含起）——本机时区
  quietEnd: 8,           // 止点（不含止）；相等 = 关闭安静时段
  nearMin: 20,           // 距最近一次对话 < near 分钟 → 命中线 ×0.15（刚聊过）
  farMin: 360,           // > far 分钟 → ×1.5（冷太久）；中间 ×1
  peakHit: 20,           // 峰时命中线 %（贵，少开口）
  valleyHit: 60,         // 谷时命中线 %（便宜，多开口）
  prompt: "好久没什么动静了……想找个话说说话",
  targetSessionID: "",   // 空 = 自动新建并复用「茶话」会话
}

const SPEAKUP_CFG = join(PLUGIN_DIR, "speakup_config.json")   // 配置（面板写、宿主读）
const SPEAKUP_LOG = join(PLUGIN_DIR, "speakup_log.json")      // 状态 + 最近 60 条掷骰记录（宿主写、面板读）

// 安静时段：含起不含止，跨午夜也行（23~7）；起 === 止 = 关闭
function inQuietHours(hour: number, cfg: SpeakUpCfg): boolean {
  const a = cfg.quietStart
  const b = cfg.quietEnd
  if (a === b) return false
  return a < b ? (hour >= a && hour < b) : (hour >= a || hour < b)
}

function bjTime(ms = Date.now()): string {
  return new Date(ms + 8 * 3600_000).toISOString().replace("T", " ").slice(0, 19)
}

function round1(n: number): number {
  return Math.round(n * 10) / 10
}

async function readSpeakUpCfg(): Promise<SpeakUpCfg> {
  const out = { ...SPEAKUP_DEFAULTS }
  try {
    const raw = JSON.parse(await readFile(SPEAKUP_CFG, "utf-8")) as Record<string, unknown>
    out.enabled = raw.enabled === true
    out.dryRun = raw.dryRun !== false
    out.intervalMin = clampNum(raw.intervalMin, 30, 1, 1440)
    out.cooldownMin = clampNum(raw.cooldownMin, 20, 0, 1440)
    out.missBoost = clampNum(raw.missBoost, 10, 0, 100)
    out.missBoostMax = clampNum(raw.missBoostMax, 40, 0, 100)
    out.quietStart = clampNum(raw.quietStart, 1, 0, 23)
    out.quietEnd = clampNum(raw.quietEnd, 8, 0, 24)
    out.nearMin = clampNum(raw.nearMin, 20, 1, 1440)
    out.farMin = clampNum(raw.farMin, 360, 1, 10080)
    out.peakHit = clampNum(raw.peakHit, 20, 0, 100)
    out.valleyHit = clampNum(raw.valleyHit, 60, 0, 100)
    if (typeof raw.prompt === "string" && raw.prompt.trim()) out.prompt = raw.prompt.trim()
    if (typeof raw.targetSessionID === "string") out.targetSessionID = raw.targetSessionID.trim()
  } catch {
    // 没有配置文件就用默认值（dryRun 默认开，安全）
  }
  return out
}

type SpeakUpState = { lastRollAt: number; lastSpeakAt: number; misses: number; sessionID: string }
type SpeakFile = { state: SpeakUpState; log: Record<string, unknown>[] }

async function loadSpeakFile(): Promise<SpeakFile> {
  try {
    const raw = JSON.parse(await readFile(SPEAKUP_LOG, "utf-8")) as { state?: Partial<SpeakUpState>; log?: unknown[] }
    return {
      state: {
        lastRollAt: clampNum(raw.state?.lastRollAt, 0, 0, Number.MAX_SAFE_INTEGER),
        lastSpeakAt: clampNum(raw.state?.lastSpeakAt, 0, 0, Number.MAX_SAFE_INTEGER),
        misses: clampNum(raw.state?.misses, 0, 0, 100000),
        sessionID: typeof raw.state?.sessionID === "string" ? raw.state.sessionID : "",
      },
      log: (Array.isArray(raw.log) ? raw.log : []).filter(
        (e) => typeof e === "object" && e !== null,
      ) as Record<string, unknown>[],
    }
  } catch {
    return { state: { lastRollAt: 0, lastSpeakAt: 0, misses: 0, sessionID: "" }, log: [] }
  }
}

async function saveSpeakFile(state: SpeakUpState, log: Record<string, unknown>[]): Promise<void> {
  const tmp = SPEAKUP_LOG + ".tmp"
  await writeFile(tmp, JSON.stringify({ state, log: log.slice(0, 60) }, null, 2), "utf-8")
  await rename(tmp, SPEAKUP_LOG)
}

async function speakRecord(file: SpeakFile, entry: Record<string, unknown>): Promise<void> {
  file.log = [{ at: bjTime(), ...entry }, ...file.log].slice(0, 60)
  await saveSpeakFile(file.state, file.log)
}

// ---------- memory 记忆（会淡忘的长期记忆；设计参考 Yuuyuko-uu/dsh-fish-memory：鲜活度半衰期 / 证据召回 / 事实键 / 每日配额）----------
type MemoryCfg = {
  enabled: boolean
  autoRecall: boolean       // hook 每轮自动召回注入
  halfLifeDays: number
  dailyMax: number
  injectMax: number
  injectMaxChars: number
  eviMin: number
  wFresh: number
  wImportance: number
  wMood: number
  wEvidence: number
  lookbackTurns: number
}

const MEM_DEFAULTS: MemoryCfg = {
  enabled: true,
  autoRecall: true,
  halfLifeDays: 30,       // 多久淡一半（天）
  dailyMax: 8,            // 每会话每天最多记几条（防把上下文塞满）
  injectMax: 6,           // 每轮最多自动想起几条
  injectMaxChars: 800,    // 每轮注入总字数封顶
  eviMin: 0.34,           // 证据门槛：命中的词不够特别就不想起
  wFresh: 0.25,           // 鲜活度权重
  wImportance: 0.25,      // 重要度权重
  wMood: 0.25,            // 情绪权重
  wEvidence: 0.25,        // 证据权重（四项归一）
  lookbackTurns: 2,       // 召回时往前取几轮用户话头当检索源
}

const MEMORY_DIR = join(PLUGIN_DIR, "memory")
const MEMORY_CFG = join(PLUGIN_DIR, "memory_cfg.json")
const MEMORY_LOG = join(PLUGIN_DIR, "memory_log.json")

type MemEntry = {
  id: string
  ts: number
  at: string
  date?: string        // 记的是哪天的旧事（可选，YYYY-MM-DD）
  text: string
  kind: "event" | "fact"   // fact = 画像/喜好/约定这类一直成立的事，不参与淡忘
  importance: number   // 1..5
  mood: number         // -2..2
  keys: string[]       // 事实键「对象.方面」，如「她.作息」；同键只留最新
  words: string[]      // 检索词
  supersededBy?: string
}

type MemFile = {
  entries: MemEntry[]            // 最新在前
  days: Record<string, number>   // 北京日期 → 该会话当日已记条数（配额记账）
}

type MemLog = {
  recalls: Record<string, unknown>[]   // 召回明细：注入了谁、谁没进、为什么
  writes: Record<string, unknown>[]    // 写入流水
}

function clampFloat(value: unknown, fallback: number, min: number, max: number): number {
  const n = Number(value)
  if (Number.isNaN(n)) return fallback
  return Math.min(max, Math.max(min, n))
}

// 权重四项：负数当 0，全 0 回默认；总量非 1 按比例归一（照原版「四项合计 100%」）
function normWeights(raw: Record<string, unknown>): { wFresh: number; wImportance: number; wMood: number; wEvidence: number } {
  const f = clampFloat(raw.wFresh, MEM_DEFAULTS.wFresh, 0, 1)
  const i = clampFloat(raw.wImportance, MEM_DEFAULTS.wImportance, 0, 1)
  const m = clampFloat(raw.wMood, MEM_DEFAULTS.wMood, 0, 1)
  const e = clampFloat(raw.wEvidence, MEM_DEFAULTS.wEvidence, 0, 1)
  const sum = f + i + m + e
  if (sum <= 0) return { wFresh: 0.25, wImportance: 0.25, wMood: 0.25, wEvidence: 0.25 }
  const r = (x: number): number => Math.round((x / sum) * 100) / 100
  return { wFresh: r(f), wImportance: r(i), wMood: r(m), wEvidence: r(e) }
}

async function readMemCfg(): Promise<MemoryCfg> {
  const out = { ...MEM_DEFAULTS }
  try {
    const raw = JSON.parse(await readFile(MEMORY_CFG, "utf-8")) as Record<string, unknown>
    out.enabled = raw.enabled !== false
    out.autoRecall = raw.autoRecall !== false
    out.halfLifeDays = clampNum(raw.halfLifeDays, 30, 1, 3650)
    out.dailyMax = clampNum(raw.dailyMax, 8, 1, 100)
    out.injectMax = clampNum(raw.injectMax, 6, 1, 20)
    out.injectMaxChars = clampNum(raw.injectMaxChars, 800, 100, 4000)
    out.eviMin = clampFloat(raw.eviMin, MEM_DEFAULTS.eviMin, 0, 0.9)
    out.lookbackTurns = clampNum(raw.lookbackTurns, 2, 1, 10)
    Object.assign(out, normWeights(raw))
  } catch {
    // 没配置文件 → 全默认
  }
  return out
}

// sessionID → 安全文件名（防路径穿越；OpenCode 的 ses_* 本来就干净，工具侧仍防御）
function memFileFor(sessionID: string): string {
  return join(MEMORY_DIR, sessionID.replace(/[^a-zA-Z0-9_-]/g, "_") + ".json")
}

async function loadMemFile(sessionID: string): Promise<MemFile> {
  try {
    const raw = JSON.parse(await readFile(memFileFor(sessionID), "utf-8")) as { entries?: unknown; days?: unknown }
    const entries = (Array.isArray(raw.entries) ? raw.entries : []).filter(
      (e): e is MemEntry =>
        typeof e === "object" && e !== null &&
        typeof (e as MemEntry).text === "string" && typeof (e as MemEntry).ts === "number",
    )
    return { entries, days: (typeof raw.days === "object" && raw.days !== null ? raw.days : {}) as Record<string, number> }
  } catch {
    return { entries: [], days: {} }
  }
}

// 原子写 + 改前备份（照原版：每次写入前把旧文件留一份 .bak）
async function saveMemFile(sessionID: string, file: MemFile): Promise<void> {
  const p = memFileFor(sessionID)
  await mkdir(MEMORY_DIR, { recursive: true })
  try {
    await copyFile(p, p + ".bak")
  } catch {
    // 首次写入还没有旧文件，无需备份
  }
  const tmp = p + ".tmp"
  await writeFile(tmp, JSON.stringify({ entries: file.entries.slice(0, 500), days: file.days }, null, 2), "utf-8")
  await rename(tmp, p)
}

async function saveMemLog(log: MemLog): Promise<void> {
  const tmp = MEMORY_LOG + ".tmp"
  await writeFile(tmp, JSON.stringify({ recalls: log.recalls.slice(0, 12), writes: log.writes.slice(0, 12) }, null, 2), "utf-8")
  await rename(tmp, MEMORY_LOG)
}

async function loadMemLog(): Promise<MemLog> {
  try {
    const raw = JSON.parse(await readFile(MEMORY_LOG, "utf-8")) as { recalls?: unknown[]; writes?: unknown[] }
    const pick = (v: unknown): Record<string, unknown>[] =>
      (Array.isArray(v) ? v : []).filter((e) => typeof e === "object" && e !== null) as Record<string, unknown>[]
    return { recalls: pick(raw.recalls), writes: pick(raw.writes) }
  } catch {
    return { recalls: [], writes: [] }
  }
}

async function memLogAppend(kind: "recalls" | "writes", entry: Record<string, unknown>): Promise<void> {
  const log = await loadMemLog()
  log[kind] = [{ at: bjTime(), ...entry }, ...log[kind]]
  await saveMemLog(log)
}

// 检索分词：CJK 连续段滑 2-gram（加整段做奖励），拉丁词按词取——零词表零依赖（原版带 jieba 词表 ~900KB，这里不引包）
function tokensOf(text: string): string[] {
  const out = new Set<string>()
  const re = /[\u4e00-\u9fff]+|[a-zA-Z0-9_]{2,}/g
  for (const seg of text.match(re) || []) {
    if (/^[a-zA-Z0-9_]+$/.test(seg)) {
      out.add(seg.toLowerCase())
    } else if (seg.length <= 2) {
      out.add(seg)
    } else {
      for (let i = 0; i + 2 <= seg.length; i++) out.add(seg.slice(i, i + 2))
      out.add(seg)
    }
  }
  return [...out]
}

// 鲜活度：0.5^(年龄天数/半衰期)，只影响排序，不删内容（照原版「不删，只是越来越难被想起来」）
function freshness(e: MemEntry, now: number, halfLifeDays: number): number {
  return Math.pow(0.5, Math.max(0, now - e.ts) / 86_400_000 / halfLifeDays)
}

type Recalled = { e: MemEntry; score: number; evi: number }

// 召回核心：证据（命中词越独特越好 × 覆盖）+ 鲜活度/重要度/情绪 加权；eviMin 门槛过滤；字数封顶
function recallCore(file: MemFile, query: string, cfg: MemoryCfg, limit: number): { events: Recalled[]; facts: MemEntry[]; skipped: { id: string; why: string }[] } {
  const now = Date.now()
  const alive = file.entries.filter((e) => !e.supersededBy)
  // 长期事实：固定带出，不看这句问了什么，也不占 injectMax 的位置（照原版）
  const facts = alive
    .filter((e) => e.kind === "fact")
    .sort((a, b) => (b.importance - a.importance) || (b.ts - a.ts))
    .slice(0, 3)
  const events = alive.filter((e) => e.kind !== "fact")
  const skipped: { id: string; why: string }[] = []
  if (events.length === 0 || !query.trim()) return { events: [], facts, skipped }

  const qTokens = tokensOf(query)
  if (qTokens.length === 0) return { events: [], facts, skipped }
  // 独特度走文档频率：词在库里出现得越少越「特别」——低频词才是身份线索（原版「车不撞进车站」的零词典版）
  const df = new Map<string, number>()
  for (const e of events) {
    for (const t of new Set([...e.words, ...tokensOf(e.text)])) df.set(t, (df.get(t) || 0) + 1)
  }
  const total = Math.max(1, events.length)
  const scored = events.map((e) => {
    const eviTokens = new Set([...e.words.map((w) => w.toLowerCase()), ...tokensOf(e.text)])
    let evi = 0
    let hits = 0
    for (const q of qTokens) {
      if (!eviTokens.has(q)) continue
      hits++
      const freq = Math.min(1, (df.get(q) || 0) / Math.sqrt(total + 1))
      evi = Math.max(evi, 0.4 + 0.6 * (1 - freq))
    }
    evi = evi > 0 ? evi * Math.min(1, hits / 2) : 0
    const fresh = freshness(e, now, cfg.halfLifeDays)
    const impt = (Math.min(5, Math.max(1, e.importance)) - 1) / 4
    const mood = (Math.min(2, Math.max(-2, e.mood)) + 2) / 4
    const score = cfg.wFresh * fresh + cfg.wImportance * impt + cfg.wMood * mood + cfg.wEvidence * evi
    return { e, score: round1(score * 100) / 100, evi: round1(evi * 100) / 100 }
  })
  const ok = scored.filter((s) => s.evi >= cfg.eviMin).sort((a, b) => b.score - a.score)
  for (const s of scored.filter((s) => s.evi < cfg.eviMin).sort((a, b) => b.evi - a.evi).slice(0, 3)) {
    skipped.push({ id: s.e.id, why: "证据不足（evi=" + s.evi + " < 门槛 " + cfg.eviMin + "）" })
  }
  // 字数封顶：超了这次先进不来（原版同语义：下次可能就进了）
  const picked: Recalled[] = []
  let chars = 0
  for (const s of ok) {
    if (picked.length >= limit) break
    if (chars + s.e.text.length > cfg.injectMaxChars) {
      skipped.push({ id: s.e.id, why: "本轮注入字数已满" })
      continue
    }
    picked.push(s)
    chars += s.e.text.length
  }
  return { events: picked, facts, skipped }
}

// remember 工具：记一件值得留的事。配额/去重/事实键顶替都在这把关
async function memRemember(input: unknown, sessionID: string): Promise<{ content: string }> {
  const cfg = await readMemCfg()
  if (!cfg.enabled) return { content: "记忆功能已关闭（memory_cfg.json 的 enabled=false），没写。" }
  const o = (typeof input === "object" && input !== null ? input : {}) as Record<string, unknown>
  const text = String(o.text || "").trim()
  if (!text) return { content: "text 为空，没记。" }
  const kind = o.kind === "fact" ? "fact" : "event"
  const keys = (Array.isArray(o.keys) ? o.keys : []).filter((k): k is string => typeof k === "string" && k.includes(".")).slice(0, 3)
  const words = (Array.isArray(o.words) ? o.words : []).filter((w): w is string => typeof w === "string" && w.trim()).map((w) => w.trim()).slice(0, 8)
  const importance = clampNum(o.importance, 2, 1, 5)
  const mood = clampNum(o.mood, 0, -2, 2)
  const file = await loadMemFile(sessionID)
  // 近似去重：与最近 60 条里前 24 字相同 → 视为重复，不重复占配额
  if (file.entries.slice(0, 60).some((x) => x.text.slice(0, 24) === text.slice(0, 24))) {
    return { content: "这条已经记过了，没重复写。" }
  }
  const today = new Date(Date.now() + 8 * 3600_000).toISOString().slice(0, 10)
  const done = file.days[today] || 0
  if (done >= cfg.dailyMax) {
    return { content: "今天已记 " + done + "/" + cfg.dailyMax + " 条，额度满了。明天自动恢复；太琐碎的事不记也没关系。" }
  }
  const id = "m" + Date.now().toString(36) + Math.floor(Math.random() * 1e6).toString(36)
  const entry: MemEntry = {
    id,
    ts: Date.now(),
    at: bjTime(),
    date: typeof o.date === "string" && /^\d{4}-\d{2}-\d{2}$/.test(o.date) ? o.date : undefined,
    text,
    kind,
    importance,
    mood,
    keys,
    words,
  }
  // 事实键顶掉旧条：同键同 kind = 同一件事，只留最新（旧的标 superseded，不删）
  if (keys.length) {
    for (const e of file.entries) {
      if (!e.supersededBy && e.kind === kind && e.keys.some((k) => keys.includes(k))) e.supersededBy = id
    }
  }
  file.entries.unshift(entry)
  file.days[today] = done + 1
  await saveMemFile(sessionID, file)
  await memLogAppend("writes", { type: "remember", sessionID, text: text.slice(0, 60), kind, importance, keys })
  return { content: "已记下：" + text.slice(0, 40) + (text.length > 40 ? "…" : "") + "（" + (kind === "fact" ? "长期事实" : "事件") + "，重要 " + importance + "，今日 " + (done + 1) + "/" + cfg.dailyMax + "）" }
}

// recall 工具：主动想起。格式与 hook 注入一致
async function memRecall(query: string, sessionID: string): Promise<{ content: string }> {
  const cfg = await readMemCfg()
  if (!cfg.enabled) return { content: "记忆功能已关闭。" }
  const file = await loadMemFile(sessionID)
  if (!file.entries.some((e) => !e.supersededBy)) return { content: "这个会话的记忆库还是空的。" }
  if (!query.trim()) return { content: "query 为空，没法想起。" }
  const r = recallCore(file, query, cfg, cfg.injectMax)
  const lines: string[] = []
  if (r.facts.length) {
    lines.push("【一直记得的】")
    for (const f of r.facts) lines.push("- " + f.text + (f.date ? "（" + f.date + "）" : ""))
  }
  if (r.events.length) {
    lines.push("【想起来的旧话】")
    for (const s of r.events) lines.push("- " + s.e.text + "（" + (s.e.date || s.e.at.slice(0, 10)) + "，重要 " + s.e.importance + "）")
  }
  if (lines.length === 0) lines.push("没想起贴切的旧话——可能证据不够、或者太旧已经淡下去了。")
  return { content: lines.join("\n") }
}

// 消息文本防御性提取：形状可能是 string / 带 text 字段的段数组 / content 数组
function textOfMsg(m: unknown): string {
  const o = (typeof m === "object" && m !== null ? m : {}) as Record<string, unknown>
  const info = (typeof o.info === "object" && o.info !== null ? o.info : {}) as Record<string, unknown>
  for (const src of [o, info]) {
    for (const k of ["text", "textContent", "content", "body"]) {
      const v = src[k]
      if (typeof v === "string" && v.trim()) return v
      if (Array.isArray(v)) {
        const joined = v
          .map((p) => {
            if (typeof p === "string") return p
            const q = (typeof p === "object" && p !== null ? p : {}) as Record<string, unknown>
            return typeof q.text === "string" ? q.text : ""
          })
          .join("")
        if (joined.trim()) return joined
      }
    }
  }
  return ""
}

// 召回检索源：最近 turns 轮 user 话头（hook/工具共用）
function lastUserText(messages: unknown, turns: number): string {
  const out: string[] = []
  const list = Array.isArray(messages) ? messages : []
  for (let i = list.length - 1; i >= 0 && out.length < turns; i--) {
    const m = list[i] as Record<string, unknown>
    const info = (typeof m.info === "object" && m.info !== null ? m.info : {}) as Record<string, unknown>
    const role = String(m.role || info.role || "")
    if (role !== "user") continue
    const t = textOfMsg(m)
    if (t.trim()) out.unshift(t)
  }
  return out.join("\n")
}

// hook("context") 回调主体：自动召回注入。白名单 = 茶话会话（speak-up 派动机语的那个），
// 写代码的会话不掺和；全程吞异常——hook 崩了绝不能影响对话本身
async function memInject(sessionID: string, system: { type: string; text: string }[], messages: unknown[]): Promise<void> {
  try {
    const cfg = await readMemCfg()
    if (!cfg.enabled || !cfg.autoRecall) return
    const talkID = (await loadSpeakFile()).state.sessionID
    if (!talkID || sessionID !== talkID) return
    const file = await loadMemFile(sessionID)
    if (!file.entries.some((e) => !e.supersededBy)) return
    const query = lastUserText(messages, cfg.lookbackTurns)
    const { events, facts, skipped } = recallCore(file, query, cfg, cfg.injectMax)
    if (events.length === 0 && facts.length === 0) return
    const lines: string[] = []
    if (facts.length) {
      lines.push("【一直记得的】")
      for (const f of facts) lines.push("- " + f.text + (f.date ? "（" + f.date + "）" : ""))
    }
    if (events.length) {
      lines.push("【想起来的旧话】")
      for (const s of events) lines.push("- " + s.e.text + "（" + (s.e.date || s.e.at.slice(0, 10)) + "）")
    }
    lines.push("（这些是memory 记忆自动想起的：接得上就自然用上，接不上别硬扯，无需回应本段。）")
    system.push({ type: "text", text: lines.join("\n") })
    await memLogAppend("recalls", {
      type: "recall",
      sessionID,
      query: query.slice(0, 80),
      injected: [...events.map((s) => s.e.id), ...facts.map((f) => f.id)],
      skipped: skipped.slice(0, 4),
    })
  } catch {
    // 吞掉一切：记忆失效也不能让对话报错
  }
}

export default Plugin.define({
  id: "bili-subagent",
  async setup(ctx) {
    const opts = buildOptions(ctx.options)
    console.log("[bili-subagent] loading, mode=" + opts.autoMode + ", perDay=" + opts.perDay)

    // ① 注册 Python MCP 服务器 —— 它的 20 个白名单工具会被 OpenCode 自动暴露，TS 不再注册任何工具
    await ctx.mcp.transform((editor) => {
      const existing = editor.get("bilibili-mcp")
      editor.set("bilibili-mcp", {
        type: "local" as const,
        command: [PYTHON_EXE, SERVER_PY],
        environment: {
          PYTHONUTF8: "1",
          PYTHONIOENCODING: "utf-8",
          BILI_MCP_TOOLS: ALLOWED_TOOLS,
          BILI_MCP_BUILD: "2", // 改 Python 端代码后 bump 这个值，触发 reconcile 并重启 MCP 子进程
        },
        disabled: existing ? (existing.disabled ?? false) : false,
      })
    })
    console.log("[bili-subagent] MCP server 'bilibili-mcp' registered")

    // ④ memory 记忆：注册原生工具 memory_remember / memory_recall（ctx.tool.transform）
    await ctx.tool.transform((editor) => {
      editor.add({
        name: "remember",
        description:
          "记一件值得长期记住的事（一句话，第一人称）。kind=fact 只用于画像/喜好/约定这类一直成立的事（不会随时间淡忘），其余用默认 event。" +
          "keys 是事实键「对象.方面」（如「她.作息」），同一键以后再记会顶掉旧的；键里不许写具体值，拿不准就别填。" +
          "words 是检索词，帮你以后想起来，有空就顺手给两个。每天有配额（满了会拒绝，防止上下文塞满）。",
        input: {
          type: "object",
          properties: {
            text: { type: "string", description: "一句话，第一人称" },
            kind: { type: "string", enum: ["event", "fact"], description: "event=带时间的经历（默认）；fact=长期事实，不会淡" },
            importance: { type: "number", description: "重要度 1–5，默认 2" },
            mood: { type: "number", description: "-2 到 2，默认 0" },
            keys: { type: "array", items: { type: "string" }, description: "事实键「对象.方面」，如「她.身高」" },
            words: { type: "array", items: { type: "string" }, description: "检索词" },
            date: { type: "string", description: "事发生在哪天（YYYY-MM-DD），今天的就不用给" },
          },
          required: ["text"],
          additionalProperties: false,
        },
        execute: async (input, context) => memRemember(input, context.sessionID),
      })
      editor.add({
        name: "recall",
        description: "主动想起：按一段话头查记忆库，返回最贴的几条旧事（会淡忘，太旧的难想起来）。 natural 地想起就调。",
        input: {
          type: "object",
          properties: { query: { type: "string", description: "想问的话头" } },
          required: ["query"],
          additionalProperties: false,
        },
        execute: async (input, context) => memRecall(String((input as { query?: unknown }).query || ""), context.sessionID),
      })
    })
    console.log("[bili-subagent] memory tools registered (memory_remember / memory_recall)")

    // ⑤ memory 记忆：每轮模型请求前自动召回注入（hook("context")——只影响当轮请求，不污染历史）
    await ctx.session.hook("context", (event) =>
      memInject(event.sessionID, event.system as unknown as { type: string; text: string }[], event.messages as unknown[]),
    )
    console.log("[bili-subagent] memory recall hook armed")

    // ② /bili [方向] 命令：在当前会话手动触发一轮刷站
    await ctx.command.transform((editor) => {
      editor.add({
        name: "bili",
        description: "手动触发一次 B 站自主巡视（搜索→字幕→评论→内部报告），参数为想看的方向，如 `/bili AI Agent`",
        execute: async ({ sessionID, prompt }) => {
          const direction = (prompt.text || "").trim() || opts.defaultDirection
          await ctx.session.prompt({
            sessionID,
            text: buildScanPrompt(direction, opts.perScan, "手动 /bili 命令"),
            delivery: "queue",
          })
        },
      })
      editor.add({
        name: "speak",
        description: "主动搭话（不掷骰、不受冷却、不动连败计数）：默认演练 dry-run，参数带「真发」两字才真发，如 `/speak` 或 `/speak 真发`",
        execute: async ({ prompt }) => {
          const force = (prompt.text || "").indexOf("真发") >= 0
          return await manualSpeak(force)
        },
      })
    })
    console.log("[bili-subagent] command '/bili' registered")

    // ③ 谷时调度：每 60 秒 tick 一次；条件齐了才派发，派发失败退回这一轮额度
    // 配置源：bili_config.json（Run bili_config 工具即改即生效）> ctx.options > 默认值
    async function readCfgOverrides(): Promise<Partial<BiliOptions>> {
      try {
        const fs = await import("fs/promises")
        const raw = JSON.parse(await fs.readFile(join(PLUGIN_DIR, "bili_config.json"), "utf-8")) as Record<string, unknown>
        const out: Record<string, unknown> = {}
        const mode = String(raw.autoMode || "")
        if (["manual", "valley", "always"].includes(mode)) out.autoMode = mode
        if (raw.perDay !== undefined) out.perDay = clampNum(raw.perDay, 5, 0, 100)
        if (raw.perScan !== undefined) out.perScan = clampNum(raw.perScan, 3, 1, 10)
        if (raw.gapMin !== undefined) out.gapMin = clampNum(raw.gapMin, 180, 5, 720)
        if (typeof raw.defaultDirection === "string" && raw.defaultDirection.trim()) out.defaultDirection = raw.defaultDirection.trim()
        if (typeof raw.targetSessionID === "string") out.targetSessionID = raw.targetSessionID.trim()
        return out as Partial<BiliOptions>
      } catch (e) {
        return {}
      }
    }
    type Unknown = Record<string, unknown> | undefined
    const stored = (await ctx.storage.get("scheduler")) as Unknown | undefined
    const state: SchedState = {
      date: String((stored as Record<string, unknown>)?.date || ""),
      rounds: Number((stored as Record<string, unknown>)?.rounds || 0),
      lastRunAt: Number((stored as Record<string, unknown>)?.lastRunAt || 0),
      sessionID: String((stored as Record<string, unknown>)?.sessionID || ""),
    }
    const saveState = async (): Promise<void> => {
      await ctx.storage.set("scheduler", { ...state })
    }

    async function dispatch(reason: string, eff: BiliOptions = opts): Promise<void> {
      let sessionID = eff.targetSessionID || state.sessionID
      if (!sessionID) {
        const created = await ctx.session.create({ title: "bili-subagent 自主巡视" })
        state.sessionID = created.id
        await saveState()
        sessionID = created.id
      }
      await ctx.session.prompt({
        sessionID,
        text: buildScanPrompt(eff.defaultDirection, eff.perScan, reason),
        delivery: "queue",
      })
    }

    // ---- 主动搭话（setup 内，可用 ctx）----
    // 距目标会话最近一次消息的分钟数；解析不出（空会话/失败）返回 null = 当作"冷很久"
    async function lastTalkMin(sessionID: string): Promise<number | null> {
      try {
        const msgs = (await ctx.session.context({ sessionID })) as unknown
        if (!Array.isArray(msgs) || msgs.length === 0) return null
        for (let i = msgs.length - 1; i >= 0; i--) {
          const m = msgs[i] as Record<string, unknown>
          const t = (typeof m.time === "object" && m.time !== null ? m.time : {}) as Record<string, unknown>
          const raw =
            (typeof t.created === "number" && t.created) ||
            (typeof t.start === "number" && t.start) ||
            (typeof m.createdAt === "number" && m.createdAt) ||
            0
          if (raw > 0) {
            const ms = raw < 1e12 ? raw * 1000 : raw // 秒/毫秒都认
            return Math.max(0, (Date.now() - ms) / 60_000)
          }
        }
        return null
      } catch {
        return null
      }
    }

    // 真发派发：目标会话 cfg.targetSessionID 优先，其次上次记忆；都没有就新建并复用「茶话」会话
    async function speakDispatch(cfg: SpeakUpCfg, file: SpeakFile): Promise<string> {
      let sessionID = cfg.targetSessionID || file.state.sessionID
      if (!sessionID) {
        const created = await ctx.session.create({ title: "bili-subagent 茶话" })
        file.state.sessionID = created.id
        sessionID = created.id
      }
      await ctx.session.prompt({ sessionID, text: cfg.prompt, delivery: "queue" })
      return sessionID
    }

    // 每 60s tick 都进来一趟，先过 intervalMin 门（不掷骰不算），再过安静/冷却门，然后才真掷骰
    async function speakUpTick(): Promise<void> {
      const cfg = await readSpeakUpCfg()
      if (!cfg.enabled) return
      const file = await loadSpeakFile()
      const now = Date.now()
      if (file.state.lastRollAt && now - file.state.lastRollAt < cfg.intervalMin * 60_000) return
      file.state.lastRollAt = now
      await saveSpeakFile(file.state, file.log) // 只推进掷骰时间，不写日志行
      const hour = new Date(now + 8 * 3600_000).getUTCHours() // 北京小时（本机时区≈北京，仍按北京算）
      if (inQuietHours(hour, cfg)) return
      if (cfg.cooldownMin > 0 && file.state.lastSpeakAt && now - file.state.lastSpeakAt < cfg.cooldownMin * 60_000) return

      // 命中线 = min(100, base×factor + min(boostMax, misses×boost))
      const peak = !inValleyBJ()
      const base = peak ? cfg.peakHit : cfg.valleyHit
      const talkID = cfg.targetSessionID || file.state.sessionID
      const sinceTalk = talkID ? await lastTalkMin(talkID) : null
      const factor = sinceTalk === null ? 1.5 : sinceTalk < cfg.nearMin ? 0.15 : sinceTalk > cfg.farMin ? 1.5 : 1
      const line = Math.min(100, base * factor + Math.min(cfg.missBoostMax, file.state.misses * cfg.missBoost))
      const roll = Math.random() * 100
      if (roll >= line) {
        // 没掷中：连败 +1，下次概率更高
        file.state.misses += 1
        await speakRecord(file, { type: "miss", roll: round1(roll), line: round1(line), peak })
        return
      }
      // 掷中（连败清零）：dry-run 只记账不真发
      file.state.misses = 0
      if (cfg.dryRun) {
        await speakRecord(file, { type: "dry", roll: round1(roll), line: round1(line), peak })
        console.log("[bili-subagent] speak-up hit (dry-run), line=" + round1(line))
        return
      }
      try {
        const sid = await speakDispatch(cfg, file)
        file.state.lastSpeakAt = Date.now()
        await speakRecord(file, { type: "speak", roll: round1(roll), line: round1(line), peak, sessionID: sid })
        console.log("[bili-subagent] speak-up dispatched to " + sid)
      } catch (e) {
        // 派发失败：会话多半被手动删了，清掉记忆下次新建
        file.state.sessionID = ""
        await speakRecord(file, { type: "failed", roll: round1(roll), line: round1(line), error: String(e).slice(0, 200) })
        console.error("[bili-subagent] speak dispatch failed:", String(e))
      }
    }

    // /speak 手动路径：不掷骰、不受冷却、不动连败计数；默认演练，参数带「真发」才真发
    async function manualSpeak(force: boolean): Promise<string> {
      const cfg = await readSpeakUpCfg()
      const dry = !force && cfg.dryRun
      const peak = !inValleyBJ()
      const file = await loadSpeakFile()
      if (dry) {
        await speakRecord(file, { type: "manual-dry", peak })
        return "✅ 主动搭话【演练】完成（dry-run，未真发）。想真发：`/speak 真发`。连败计数不动。"
      }
      try {
        const sid = await speakDispatch(cfg, file)
        file.state.lastSpeakAt = Date.now()
        await speakRecord(file, { type: "manual-speak", peak, sessionID: sid })
        return "✅ 已真发主动搭话 → 会话 " + sid.slice(0, 8) + "…（伙伴会在队列里自然接话）"
      } catch (e) {
        file.state.sessionID = ""
        await speakRecord(file, { type: "manual-failed", error: String(e).slice(0, 200) })
        return "⚠️ 主动搭话派发失败：" + String(e).slice(0, 160)
      }
    }

    async function tick(): Promise<void> {
      const eff: BiliOptions = { ...opts, ...(await readCfgOverrides()) }
      if (eff.autoMode === "manual") return
      const bjDate = new Date(Date.now() + 8 * 3600_000).toISOString().slice(0, 10)
      if (state.date !== bjDate) {
        state.date = bjDate
        state.rounds = 0
        await saveState()
      }
      if (state.rounds >= eff.perDay) return
      if (eff.autoMode === "valley" && !inValleyBJ()) return
      if (Date.now() - state.lastRunAt < eff.gapMin * 60_000) return

      state.rounds += 1
      state.lastRunAt = Date.now()
      await saveState()
      try {
        await dispatch(eff.autoMode === "always" ? "日常巡视" : "谷时巡视", eff)
        console.log("[bili-subagent] auto scan dispatched, rounds today " + state.rounds + "/" + opts.perDay)
      } catch (e) {
        // 派发失败：退回这一轮额度；会话可能被手动删了，下次 tick 重建
        state.rounds -= 1
        state.sessionID = ""
        await saveState()
        console.error("[bili-subagent] dispatch failed, quota refunded:", String(e))
      }
    }

    const timer = setInterval(() => {
      void tick().catch((e: unknown) => console.error("[bili-subagent] tick error:", String(e)))
      void speakUpTick().catch((e: unknown) => console.error("[bili-subagent] speak tick error:", String(e)))
    }, 60_000)
    console.log("[bili-subagent] scheduler armed (interval 60s)")

    return () => {
      clearInterval(timer)
      console.log("[bili-subagent] unloaded")
    }
  },
})
