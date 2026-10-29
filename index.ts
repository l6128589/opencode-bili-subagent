// bili-subagent —— OpenCode V2 薄壳插件
// 职责只有三件，B 站所有能力都在 vendor/bilibili-mcp/mcp_server.py（Python MCP）里：
//   ① ctx.mcp.transform  → 注册 Python MCP 服务器（工具经白名单 BILI_MCP_TOOLS 裁剪，自动暴露成原生工具）
//   ② ctx.command.transform → 注册 /bili 命令：手动触发一轮「搜索→字幕→评论→内部报告」
//   ③ setInterval 谷时调度 → 到点往目标会话塞一句"该刷了"，由会话里的伙伴调 bili_scan 走完整闭环
// 卸载时清掉定时器（返回 cleanup）。

import { existsSync } from "node:fs"
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
    }, 60_000)
    console.log("[bili-subagent] scheduler armed (interval 60s)")

    return () => {
      clearInterval(timer)
      console.log("[bili-subagent] unloaded")
    }
  },
})
