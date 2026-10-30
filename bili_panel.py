# -*- coding: utf-8 -*-
"""
bili-subagent 网页面板（零依赖：只用 venv 里已有的 aiohttp）
  状态卡（登录/额度/谷时/最近扫描）+ 配置表单（即改即生效）+ 手动扫描 + 报告阅读器
用法：.venv\Scripts\python.exe bili_panel.py   →  http://127.0.0.1:17178
注意：只监听 127.0.0.1，不对局域网开放。
"""
import asyncio
import json
import os
import re
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from aiohttp import web

PLUGIN_HOME = Path(__file__).parent
sys.path.insert(0, str(PLUGIN_HOME / "vendor" / "bilibili-mcp"))
import mcp_server as ms  # noqa: E402  复用它的 config/state/report 逻辑

PORT = 17178
_BJ = timezone(timedelta(hours=8))

_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>bili-subagent 面板</title>
<style>
:root { color-scheme: dark; }
* { box-sizing: border-box; }
body { margin: 0; font-family: "Microsoft YaHei", system-ui; background: #11131a; color: #dfe2ea; }
.wrap { max-width: 980px; margin: 0 auto; padding: 18px; }
h1 { font-size: 20px; margin: 0 0 14px; color: #fb7299; }
.card { background: #1a1d27; border: 1px solid #2a2e3d; border-radius: 10px; padding: 14px 16px; margin-bottom: 14px; }
.grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 10px; }
.kv { background: #14161f; border-radius: 8px; padding: 10px 12px; }
.kv .k { font-size: 12px; color: #8b90a3; }
.kv .v { font-size: 18px; margin-top: 2px; }
.ok { color: #7ee787; } .bad { color: #ff7b72; } .warn { color: #e3b341; }
form { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 10px 14px; }
label { display: block; font-size: 12px; color: #8b90a3; margin-bottom: 4px; }
input, select { width: 100%; background: #14161f; border: 1px solid #2a2e3d; color: #dfe2ea; border-radius: 6px; padding: 7px 9px; font-size: 13px; }
button { background: #fb7299; border: 0; color: #fff; border-radius: 7px; padding: 8px 16px; font-size: 13px; cursor: pointer; }
button.ghost { background: #2a2e3d; }
button:disabled { opacity: .5; cursor: default; }
.row { display: flex; gap: 10px; align-items: end; margin-bottom: 14px; flex-wrap: wrap; }
.row > div { flex: 1; min-width: 160px; }
#toast { position: fixed; right: 18px; bottom: 18px; background: #234; color: #dfe2ea; padding: 10px 14px; border-radius: 8px; opacity: 0; transition: opacity .25s; }
.reports { display: flex; gap: 14px; align-items: flex-start; flex-wrap: wrap; }
.reports ul { list-style: none; margin: 0; padding: 0; min-width: 140px; }
.reports li { padding: 6px 8px; border-radius: 6px; cursor: pointer; font-size: 13px; color: #9aa0b5; }
.reports li:hover { background: #232736; }
.reports li.on { background: #fb729922; color: #fb7299; }
pre { white-space: pre-wrap; word-break: break-word; flex: 1; min-width: 320px; margin: 0; font-size: 13px; line-height: 1.55; background: #14161f; border-radius: 8px; padding: 12px; max-height: 60vh; overflow: auto; }
.hint { font-size: 12px; color: #8b90a3; margin-top: 6px; }
ul.smalllog { list-style: none; margin: 10px 0 0; padding: 0; max-height: 210px; overflow: auto; }
ul.smalllog li { font-size: 12px; color: #9aa0b5; padding: 3px 0; border-bottom: 1px solid #20242f; }
</style>
</head>
<body>
<div class="wrap">
  <h1>bili-subagent 面板</h1>

  <div class="card"><div class="grid" id="kv"></div></div>

  <div class="card">
    <div class="row">
      <div><label>想看的方向（空=用默认）</label><input id="scanDir" placeholder="如：AI Agent"></div>
      <div style="max-width:120px"><label>条数</label><input id="scanCount" type="number" min="1" max="10" value="3"></div>
      <div><button id="scanBtn" onclick="doScan()">手动刷一轮</button></div>
    </div>
  </div>

  <div class="card">
    <div class="hint" id="curCfg">当前生效：正在读取…</div>
    <form id="cfgForm" onsubmit="saveCfg(); return false">
      <div><label>自动模式</label><select id="f_autoMode">
        <option value="manual">manual 只手动</option><option value="valley">valley 只在谷时</option><option value="always">always 全天</option></select></div>
      <div><label>私信意愿 dmMode</label><select id="f_dmMode">
        <option value="off">off 不发私信</option><option value="only">only 只私信</option><option value="both">both 都发</option></select></div>
      <div><label>默认方向</label><input id="f_defaultDirection"></div>
      <div><label>每天最多几轮</label><input id="f_perDay" type="number" min="0" max="100"></div>
      <div><label>每轮挑几条</label><input id="f_perScan" type="number" min="1" max="10"></div>
      <div><label>两轮至少间隔(分)</label><input id="f_gapMin" type="number" min="5" max="720"></div>
      <div><label>播放量下限</label><input id="f_minViews" type="number" min="0"></div>
      <div><label>至少发布天数</label><input id="f_minAgeDays" type="number" min="0"></div>
      <div><button type="submit">保存配置（调度器下一 tick 生效）</button></div>
    </form>
    <div class="hint">谷时＝工作日 9–12、14–18 之外（含整个周末），DeepSeek 定价口径，按北京时间。</div>
  </div>

  <div class="card">
    <div class="grid" id="suKv"></div>
    <form id="suForm" onsubmit="saveSU(); return false" style="margin-top: 12px">
      <div><label>功能开关</label><select id="s_enabled">
        <option value="1">开：按间隔自动掷骰</option><option value="0">关：彻底不掷</option></select></div>
      <div><label>演练模式 dry-run</label><select id="s_dryRun">
        <option value="1">开：掷中只记账不真发</option><option value="0">关：掷中就真派</option></select></div>
      <div><label>掷骰间隔(分)</label><input id="s_intervalMin" type="number" min="1" max="1440"></div>
      <div><label>开口后冷却(分)</label><input id="s_cooldownMin" type="number" min="0" max="1440"></div>
      <div><label>没中加成 × 次数</label><input id="s_missBoost" type="number" min="0" max="100"></div>
      <div><label>加成封顶</label><input id="s_missBoostMax" type="number" min="0" max="100"></div>
      <div><label>安静时段起(时)</label><input id="s_quietStart" type="number" min="0" max="23"></div>
      <div><label>安静时段止(时)</label><input id="s_quietEnd" type="number" min="0" max="24"></div>
      <div><label>刚聊过阈值(分)</label><input id="s_nearMin" type="number" min="1" max="1440"></div>
      <div><label>冷太久阈值(分)</label><input id="s_farMin" type="number" min="1" max="10080"></div>
      <div><label>峰时命中率 %</label><input id="s_peakHit" type="number" min="0" max="100"></div>
      <div><label>谷时命中率 %</label><input id="s_valleyHit" type="number" min="0" max="100"></div>
      <div style="grid-column: 1 / -1"><label>开口动机语（掷中后送进茶话会话的一句话）</label><input id="s_prompt"></div>
      <div><button type="submit">保存主动搭话（下一 tick 生效）</button></div>
      <div><button type="button" class="ghost" onclick="toast('掷骰和冷却都不管：在 OpenCode 会话里敲 /speak 演练、/speak 真发 即时开口')">手动开口在日常对话里</button></div>
    </form>
    <ul class="smalllog" id="suLog"></ul>
    <div class="hint">掷到线下才开口：命中线 = min(100, 峰<b id="suhPeak">?</b>% / 谷<b id="suhValley">?</b>% × 时机因子 + min(封顶, 连败×加成))。安静时段（含起不含止）与冷却只推时间不记日志。手动开口用 /speak。</div>
  </div>

  <div class="card">
    <div class="grid" id="mKv"></div>
    <form id="mForm" onsubmit="saveMem(); return false" style="margin-top: 12px">
      <div><label>记忆开关</label><select id="m_enabled">
        <option value="1">开：记账 + 召回</option><option value="0">关：全部停用</option></select></div>
      <div><label>自动召回注入</label><select id="m_autoRecall">
        <option value="1">开：每轮自动想起（仅茶话会话）</option><option value="0">关：只手动 recall</option></select></div>
      <div><label>半衰期(天)</label><input id="m_halfLifeDays" type="number" min="1" max="3650"></div>
      <div><label>每天最多记几条</label><input id="m_dailyMax" type="number" min="1" max="100"></div>
      <div><label>每轮最多想起几条</label><input id="m_injectMax" type="number" min="1" max="20"></div>
      <div><label>注入字数封顶</label><input id="m_injectMaxChars" type="number" min="100" max="4000"></div>
      <div><label>证据门槛 eviMin</label><input id="m_eviMin" type="number" min="0" max="0.9" step="0.01"></div>
      <div><label>检索回看几轮</label><input id="m_lookbackTurns" type="number" min="1" max="10"></div>
      <div><label>鲜活度权重</label><input id="m_wFresh" type="number" min="0" max="1" step="0.05"></div>
      <div><label>重要度权重</label><input id="m_wImportance" type="number" min="0" max="1" step="0.05"></div>
      <div><label>情绪权重</label><input id="m_wMood" type="number" min="0" max="1" step="0.05"></div>
      <div><label>证据权重</label><input id="m_wEvidence" type="number" min="0" max="1" step="0.05"></div>
      <div><button type="submit">保存 memory 配置（下一轮生效）</button></div>
    </form>
    <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 14px; margin-top: 12px">
      <div><div class="k" style="font-size: 12px; color: #8b90a3">最近记住的（≤12 条，跨会话）</div><ul class="smalllog" id="mList"></ul></div>
      <div><div class="k" style="font-size: 12px; color: #8b90a3">最近想起 / 写入流水</div><ul class="smalllog" id="mAct"></ul></div>
    </div>
    <div class="row" style="margin-top: 12px">
      <div><label>旧档案路径（md / txt 等，往下找两层）</label><input id="m_path" placeholder="如 D:\notes\她的记忆档案.md"></div>
      <div style="max-width: 100px"><label>扫描</label><button type="button" onclick="doMemScan()">扫描出候选</button></div>
      <div style="max-width: 110px"><label>导入</label><button type="button" class="ghost" onclick="doMemImport()">导入选中</button></div>
    </div>
    <ul class="smalllog" id="mCands"></ul>
    <div class="hint">memory 记忆会淡忘（半衰期只改排序、不删数据）。旧档案导入＝拆成一行一条直入茶话会话记库（轻量、不调模型；第一人称改写这类润色交给日常对话里的伙伴）。调权重四项自动归一成 100%。</div>
  </div>

  <div class="card reports">
    <ul id="repList"></ul>
    <pre id="repView">← 点左边的日期看当天内部报告</pre>
  </div>
</div>
<div id="toast"></div>
<script>
const $ = (id) => document.getElementById(id);
let curDate = "";
let formDirty = false;   // 用户正在编辑表单时，轮询不覆盖（修「改完自动跳回去」的坑）

// 编辑表单一有输入就标记 dirty，轮询 fillCfg 让位
document.querySelectorAll("#cfgForm input, #cfgForm select").forEach((el) => {
  el.addEventListener("input", () => { formDirty = true; });
});

let suDirty = false;   // 主动搭话表单同理：编辑中轮询不覆盖
document.querySelectorAll("#suForm input, #suForm select").forEach((el) => {
  el.addEventListener("input", () => { suDirty = true; });
});

function toast(msg) { const t = $("toast"); t.textContent = msg; t.style.opacity = 1; setTimeout(() => t.style.opacity = 0, 2200); }
function esc(s) { return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;"); }

async function doScan() {
  const btn = $("scanBtn"); btn.disabled = true;
  try {
    const r = await fetch("/api/scan", { method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({ direction: $("scanDir").value, count: Number($("scanCount").value) || 3 }) });
    const d = await r.json();
    toast(d.success ? "已开跑，写完盘刷新列表就能看到" : ("没跑起来：" + (d.error || "重入")));
    if (d.success) setTimeout(loadState, 4000);
  } catch (e) { toast("请求失败：" + e); } finally { btn.disabled = false; }
}

function fillCfg(c) {
  $("f_autoMode").value = c.autoMode; $("f_dmMode").value = c.dmMode;
  $("f_defaultDirection").value = c.defaultDirection;
  $("f_perDay").value = c.perDay; $("f_perScan").value = c.perScan;
  $("f_gapMin").value = c.gapMin; $("f_minViews").value = c.minViews; $("f_minAgeDays").value = c.minAgeDays;
  formDirty = false;   // 表单和真实配置对齐了，轮询可以恢复接管
}

const MODE_CN = { manual: "只手动", valley: "谷时自动（工作日 9–12、14–18 不跑）", always: "全天自动" };
const DM_CN = { off: "不发私信", only: "只私信", both: "评论+私信" };

function showCurrent(c) {
  // 「当前生效」只读真相：永远来自服务器，不掺表单的编辑状态
  $("curCfg").innerHTML = "当前生效：<b>" + esc(MODE_CN[c.autoMode] || c.autoMode) + "</b>"
    + " · 每天 " + c.perDay + " 轮 · 每轮 " + c.perScan + " 条 · 间隔 " + c.gapMin + " 分"
    + " · 方向「" + esc(c.defaultDirection) + "」· 私信 " + (DM_CN[c.dmMode] || c.dmMode); }

const SU_TYPE_CN = { miss: "没掷中", dry: "掷中·演练", speak: "掷中·真发", failed: "真发失败", "manual-dry": "手动演练", "manual-speak": "手动真发", "manual-failed": "手动失败" };

function fmtT(ms) {   // 毫秒 → 北京时间串（零反斜杠写法：不用正则、不用转义）
  if (!ms) return "—";
  return new Date(ms + 8 * 3600 * 1000).toISOString().slice(0, 19).replace("T", " ");
}

function fillSU(c) {   // 服务器返回的落盘结果回填，所见即所存
  $("s_enabled").value = c.enabled ? "1" : "0";
  $("s_dryRun").value = c.dryRun ? "1" : "0";
  $("s_intervalMin").value = c.intervalMin; $("s_cooldownMin").value = c.cooldownMin;
  $("s_missBoost").value = c.missBoost; $("s_missBoostMax").value = c.missBoostMax;
  $("s_quietStart").value = c.quietStart; $("s_quietEnd").value = c.quietEnd;
  $("s_nearMin").value = c.nearMin; $("s_farMin").value = c.farMin;
  $("s_peakHit").value = c.peakHit; $("s_valleyHit").value = c.valleyHit;
  $("s_prompt").value = c.prompt;
  $("suhPeak").textContent = c.peakHit; $("suhValley").textContent = c.valleyHit;
  suDirty = false;
}

function showSU(su) {   // 状态卡永远用服务器真相，不掺表单编辑值
  const st = su.state || {}, c = su.cfg || {};
  const kvv = [
    ["搭话开关", c.enabled ? '<span class="ok">开</span>' : '<span class="bad">关</span>'],
    ["演练模式", c.dryRun ? '<span class="warn">dry-run 只记账</span>' : '<span class="ok">真发</span>'],
    ["连续没中", (st.misses || 0) + " 次"],
    ["最近掷骰", fmtT(st.lastRollAt)],
    ["最近开口", fmtT(st.lastSpeakAt)],
    ["目标会话", st.sessionID ? String(st.sessionID).slice(0, 8) + "…" : "自动新建（茶话）"],
  ];
  $("suKv").innerHTML = kvv.map(([k, v]) => '<div class="kv"><div class="k">' + k + '</div><div class="v">' + v + '</div></div>').join("");
}

function renderSuLog(log) {   // 最近掷骰记录：DOM 构建，不拼 onclick、不吃转义
  const ul = $("suLog");
  ul.innerHTML = "";
  (log || []).slice(0, 12).forEach((e) => {
    const li = document.createElement("li");
    let s = e.at + "  " + (SU_TYPE_CN[e.type] || e.type || "?");
    if (typeof e.roll === "number") s += "（掷 " + e.roll + " / 线 " + e.line + "）";
    if (e.peak) s += " [峰时]";
    if (e.error) s += "：" + e.error;
    li.textContent = s;
    ul.appendChild(li);
  });
}

async function saveSU() {
  const body = {
    enabled: $("s_enabled").value === "1",
    dryRun: $("s_dryRun").value === "1",
    intervalMin: Number($("s_intervalMin").value), cooldownMin: Number($("s_cooldownMin").value),
    missBoost: Number($("s_missBoost").value), missBoostMax: Number($("s_missBoostMax").value),
    quietStart: Number($("s_quietStart").value), quietEnd: Number($("s_quietEnd").value),
    nearMin: Number($("s_nearMin").value), farMin: Number($("s_farMin").value),
    peakHit: Number($("s_peakHit").value), valleyHit: Number($("s_valleyHit").value),
    prompt: $("s_prompt").value,
  };
  try {
    const r = await fetch("/api/speakup", { method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body) });
    const d = await r.json();
    if (d.success) { fillSU(d.config); toast("主动搭话已保存（intervalMin=" + d.config.intervalMin + "），下一 tick 生效"); }
    else toast("主动搭话保存失败");
  } catch (e) { toast("请求失败：" + e); }
}

let memDirty = false;   // memory 表单同理：编辑中轮询不覆盖
document.querySelectorAll("#mForm input, #mForm select").forEach((el) => {
  el.addEventListener("input", () => { memDirty = true; });
});

function fillMem(c) {
  $("m_enabled").value = c.enabled ? "1" : "0";
  $("m_autoRecall").value = c.autoRecall ? "1" : "0";
  $("m_halfLifeDays").value = c.halfLifeDays; $("m_dailyMax").value = c.dailyMax;
  $("m_injectMax").value = c.injectMax; $("m_injectMaxChars").value = c.injectMaxChars;
  $("m_eviMin").value = c.eviMin; $("m_lookbackTurns").value = c.lookbackTurns;
  $("m_wFresh").value = c.wFresh; $("m_wImportance").value = c.wImportance;
  $("m_wMood").value = c.wMood; $("m_wEvidence").value = c.wEvidence;
  memDirty = false;
}

function showMem(m) {
  const c = m.cfg || {};
  const kvv = [
    ["记忆开关", c.enabled ? '<span class="ok">开</span>' : '<span class="bad">关</span>'],
    ["自动召回", c.autoRecall ? '<span class="ok">每轮想起（茶话）</span>' : '<span class="warn">只手动 recall</span>'],
    ["半衰期", (c.halfLifeDays || 30) + " 天淡一半"],
    ["每日配额", (c.dailyMax || 8) + " 条/会话"],
    ["记忆条数", (m.total || 0) + " 条 / " + (m.files || 0) + " 个会话"],
    ["证据门槛", "evi≥" + (c.eviMin !== undefined ? c.eviMin : 0.34)],
  ];
  $("mKv").innerHTML = kvv.map(([k, v]) => '<div class="kv"><div class="k">' + k + '</div><div class="v">' + v + '</div></div>').join("");
}

function renderMemList(list) {
  const ul = $("mList");
  ul.innerHTML = "";
  (list || []).slice(0, 12).forEach((e) => {
    const li = document.createElement("li");
    li.textContent = (e.date || String(e.at || "").slice(0, 10)) + "  " + (e.kind === "fact" ? "[长期] " : "") + String(e.text || "").slice(0, 44);
    ul.appendChild(li);
  });
  if (!ul.children.length) {
    const li = document.createElement("li");
    li.textContent = "还没有记下任何一条——在会话里让伙伴顺手调 remember";
    ul.appendChild(li);
  }
}

function renderMemAct(recalls, writes) {
  const ul = $("mAct");
  ul.innerHTML = "";
  const items = [];
  (recalls || []).slice(0, 7).forEach((r) => items.push(r.at + " 想起「" + String(r.query || "").slice(0, 16) + "」→ " + ((r.injected || []).length) + " 条"));
  (writes || []).slice(0, 4).forEach((w) => items.push(w.at + " 记下：" + String(w.text || "").slice(0, 18) + (w.type === "import" ? "（导入 " + (w.imported || 0) + " 条）" : "")));
  items.slice(0, 12).forEach((s) => { const li = document.createElement("li"); li.textContent = s; ul.appendChild(li); });
  if (!ul.children.length) {
    const li = document.createElement("li");
    li.textContent = "还没有召回 / 写入流水";
    ul.appendChild(li);
  }
}

async function saveMem() {
  const body = {
    enabled: $("m_enabled").value === "1",
    autoRecall: $("m_autoRecall").value === "1",
    halfLifeDays: Number($("m_halfLifeDays").value), dailyMax: Number($("m_dailyMax").value),
    injectMax: Number($("m_injectMax").value), injectMaxChars: Number($("m_injectMaxChars").value),
    eviMin: Number($("m_eviMin").value), lookbackTurns: Number($("m_lookbackTurns").value),
    wFresh: Number($("m_wFresh").value), wImportance: Number($("m_wImportance").value),
    wMood: Number($("m_wMood").value), wEvidence: Number($("m_wEvidence").value),
  };
  try {
    const r = await fetch("/api/memory", { method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body) });
    const d = await r.json();
    if (d.success) { fillMem(d.config); toast("memory 配置已保存（权重已归一），下一轮生效"); }
    else toast("memory 保存失败");
  } catch (e) { toast("请求失败：" + e); }
}

let memCands = [];   // 扫描出的候选行
function renderCands() {
  const ul = $("mCands");
  ul.innerHTML = "";
  memCands.slice(0, 200).forEach((t) => {
    const li = document.createElement("li");
    const box = document.createElement("input");
    box.type = "checkbox"; box.checked = true;
    box.style.width = "auto"; box.style.marginRight = "8px";
    const span = document.createElement("span");
    span.textContent = t;
    li.appendChild(box); li.appendChild(span);
    ul.appendChild(li);
  });
}

async function doMemScan() {
  const path = $("m_path").value.trim();
  if (!path) { toast("先填旧档案路径"); return; }
  try {
    const d = await (await fetch("/api/memory_scan", { method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({ path }) })).json();
    if (d.success) {
      memCands = d.candidates || [];
      renderCands();
      toast("扫到 " + memCands.length + " 条候选（" + d.files + " 个文件），勾选后导入");
    } else toast("扫描失败：" + (d.error || "未知"));
  } catch (e) { toast("请求失败：" + e); }
}

async function doMemImport() {
  const texts = [];
  document.querySelectorAll("#mCands input[type=checkbox]").forEach((b) => {
    if (b.checked && b.nextElementSibling) texts.push(b.nextElementSibling.textContent);
  });
  if (!texts.length) { toast("先扫描并勾选要导入的行"); return; }
  try {
    const d = await (await fetch("/api/memory_import", { method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({ texts }) })).json();
    if (d.success) {
      toast("已导入 " + d.imported + " 条进茶话记库（跳过重复 " + d.skipped + "）");
      $("mCands").innerHTML = "";
      memCands = [];
      loadState();
    } else toast("导入失败：" + (d.error || "未知"));
  } catch (e) { toast("请求失败：" + e); }
}

async function saveCfg() {
  const body = {
    autoMode: $("f_autoMode").value, dmMode: $("f_dmMode").value,
    defaultDirection: $("f_defaultDirection").value.trim() || "科技",
    perDay: Number($("f_perDay").value), perScan: Number($("f_perScan").value),
    gapMin: Number($("f_gapMin").value), minViews: Number($("f_minViews").value),
  };
  if ($("f_minAgeDays").value !== "") body.minAgeDays = Number($("f_minAgeDays").value);
  const r = await fetch("/api/config", { method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body) });
  const d = await r.json();
  if (d.success) {
    // 用服务器回的落盘结果回填，所见即所存
    fillCfg(d.config); formDirty = false;
    toast("已保存：" + (d.config.autoMode === "manual" ? "只手动" : d.config.autoMode === "valley" ? "谷时自动" : "全天自动") + "，调度器下一 tick 生效");
  } else {
    toast("保存失败");
  }
}

async function loadReport(date) {
  curDate = date;
  document.querySelectorAll("#repList li").forEach(li => li.classList.toggle("on", li.dataset.d === date));
  try {
    const r = await fetch("/api/report/" + date);
    $("repView").textContent = r.ok ? await r.text() : "读取失败";
  } catch (e) { $("repView").textContent = String(e); }
}

async function loadState() {
  try {
    const s = await (await fetch("/api/state")).json();
    const acc = s.account || {};
    let perm = "未检测";
    if (acc.comment_level && acc.comment_level.blocked) {
      perm = '<span class="bad">评论被等级闸</span>（' + esc(acc.comment_level.hint || "需 Lv2") + '）';
    } else if (acc.lv !== undefined) {
      perm = '<span class="ok">Lv' + acc.lv + (acc.lv >= 2 ? '，可评论 ✓' : '，暂不可评论') + '</span>';
    }
    const kv = [
      ["登录", s.loggedIn ? '<span class="ok">已登录 ✓</span>' : '<span class="bad">未登录 ✗</span>'],
      ["发言权限", perm],
      ["今日额度", s.roundsToday + " / " + s.perDay + " 轮"],
      ["时段", s.inValley ? '<span class="ok">谷时中</span>' : '<span class="warn">高峰段（valley 模式不跑）</span>'],
      ["最近扫描", esc(s.lastScanAt || "—")],
      ["已看去重", s.seenCount + " 条"],
    ];
    $("kv").innerHTML = kv.map(([k, v]) => '<div class="kv"><div class="k">' + k + '</div><div class="v">' + v + '</div></div>').join("");
    showCurrent(s.config);
    const su = s.speakUp || {};
    showSU(su);
    if (!suDirty) fillSU(su.cfg || {});
    renderSuLog(su.log);
    const mem = s.memory || {};
    showMem(mem);
    if (!memDirty) fillMem(mem.cfg || {});
    renderMemList(mem.latest);
    renderMemAct(mem.recalls, mem.writes);
    // 只有表单没在被编辑时才同步进输入框；dirty 时不打扰正在改配置的人
    if (!formDirty) fillCfg(s.config);
    const ul = $("repList");
    // 用 DOM 构建列表项：既避开 Python 字符串吞转义的坑，也不把日期拼进 onclick 属性
    ul.innerHTML = "";
    s.reports.forEach((r) => {
      const li = document.createElement("li");
      li.dataset.d = r.date;
      li.textContent = r.date + " ";
      const small = document.createElement("small");
      small.textContent = r.size + "B";
      li.appendChild(small);
      li.onclick = () => loadReport(r.date);
      ul.appendChild(li);
    });
    if (!curDate && s.reports.length) loadReport(s.reports[0].date);
  } catch (e) { toast("状态拉取失败：" + e); }
}

loadState();
setInterval(loadState, 5000);
</script>
</body>
</html>"""


def _bj_now():
    return datetime.now(_BJ)


def in_valley():
    """DeepSeek 定价口径：工作日 9-12、14-18 是高峰，其余（含整个周末）都是谷时。"""
    bj = _bj_now()
    if bj.weekday() >= 5:  # 5=周六 6=周日
        return True
    h = bj.hour
    return not ((9 <= h < 12) or (14 <= h < 18))


def _read_cfg():
    cfg = dict(ms._CONFIG_DEFAULTS)
    cfg.update({k: v for k, v in ms._load_json_file(ms.CONFIG_FILE, {}).items() if k in ms._CONFIG_DEFAULTS})
    return cfg


# ---------- 主动搭话（speakup）：独立配置文件，避开 bili_config 整文件重写丢字段的风险 ----------
SU_CFG_FILE = PLUGIN_HOME / "speakup_config.json"
SU_LOG_FILE = PLUGIN_HOME / "speakup_log.json"

_SU_DEFAULTS = {
    "enabled": True,
    "dryRun": True,          # 默认演练：掷中了也只记账不真发
    "intervalMin": 30,       # 每隔多少分钟掷一次骰子
    "cooldownMin": 20,       # 开口后冷却（分钟）
    "missBoost": 10,         # 每没中一次，下次命中线加多少
    "missBoostMax": 40,      # 加成封顶
    "quietStart": 1,         # 安静时段起点小时（含起，北京时间）
    "quietEnd": 8,           # 止点（不含止）；相等 = 关闭安静时段
    "nearMin": 20,           # 距上次对话 < near 分钟 → 命中线 ×0.15
    "farMin": 360,           # > far 分钟 → ×1.5
    "peakHit": 20,           # 峰时基础命中率 %
    "valleyHit": 60,         # 谷时基础命中率 %
    "prompt": "好久没什么动静了……想找个话说说话",
    "targetSessionID": "",   # 省 = 插件自动新建并复用「茶话」会话（面板不提供，README 说明手改）
}

# 数值字段的 clamp 区间（与 index.ts readSpeakUpCfg 的白名单一一对应）
_SU_NUM = {
    "intervalMin": (1, 1440), "cooldownMin": (0, 1440), "missBoost": (0, 100),
    "missBoostMax": (0, 100), "quietStart": (0, 23), "quietEnd": (0, 24),
    "nearMin": (1, 1440), "farMin": (1, 10080), "peakHit": (0, 100), "valleyHit": (0, 100),
}


def read_su_cfg():
    cfg = dict(_SU_DEFAULTS)
    cfg.update({k: v for k, v in ms._load_json_file(SU_CFG_FILE, {}).items() if k in _SU_DEFAULTS})
    for k, (lo, hi) in _SU_NUM.items():
        try:
            cfg[k] = int(cfg[k])
        except (TypeError, ValueError):
            cfg[k] = _SU_DEFAULTS[k]
        cfg[k] = max(lo, min(hi, cfg[k]))
    cfg["enabled"] = cfg["enabled"] is True
    cfg["dryRun"] = cfg["dryRun"] is not False   # 只有显式 false 才关，等级同 TS 端
    if not isinstance(cfg["prompt"], str) or not cfg["prompt"].strip():
        cfg["prompt"] = _SU_DEFAULTS["prompt"]
    if not isinstance(cfg["targetSessionID"], str):
        cfg["targetSessionID"] = ""
    return cfg


# ---------- memory 记忆：memory_cfg.json 配置 + memory/ 每会话一库 + memory_log.json 召回流水 ----------
MEM_CFG_FILE = PLUGIN_HOME / "memory_cfg.json"
MEM_DIR = PLUGIN_HOME / "memory"
MEM_LOG_FILE = PLUGIN_HOME / "memory_log.json"

_MEM_DEFAULTS = {
    "enabled": True,
    "autoRecall": True,      # hook 每轮自动召回注入
    "halfLifeDays": 30,      # 多久淡一半（天）
    "dailyMax": 8,           # 每会话每天最多记几条
    "injectMax": 6,          # 每轮最多注入几条
    "injectMaxChars": 800,   # 每轮注入字数封顶
    "eviMin": 0.34,          # 证据门槛
    "wFresh": 0.25,
    "wImportance": 0.25,
    "wMood": 0.25,
    "wEvidence": 0.25,
    "lookbackTurns": 2,
}
_MEM_NUM_INT = {
    "halfLifeDays": (1, 3650), "dailyMax": (1, 100), "injectMax": (1, 20),
    "injectMaxChars": (100, 4000), "lookbackTurns": (1, 10),
}

_MEM_TOK_RE = re.compile(r"[\u4e00-\u9fff]+|[a-zA-Z0-9_]{2,}")
_MEM_WORD_RE = re.compile(r"[a-zA-Z0-9_]+")
# 与 index.ts tokensOf 同口径：CJK 段 2-gram + 整段奖励，拉丁词按词
def mem_tokens(text):
    out = set()
    for seg in _MEM_TOK_RE.findall(text or ""):
        if _MEM_WORD_RE.fullmatch(seg):
            out.add(seg.lower())
        elif len(seg) <= 2:
            out.add(seg)
        else:
            for i in range(len(seg) - 1):
                out.add(seg[i:i + 2])
            out.add(seg)
    return sorted(out)


def _mem_norm_weights(cfg):
    """权重四项归一（与 TS normWeights 同口径：负数当 0，全 0 回默认，按比例归一）。"""
    vals = []
    for k in ("wFresh", "wImportance", "wMood", "wEvidence"):
        try:
            vals.append(max(0.0, float(cfg.get(k))))
        except (TypeError, ValueError):
            vals.append(0.25)
    total = sum(vals)
    if total <= 0:
        vals = [0.25, 0.25, 0.25, 0.25]
        total = 1.0
    for k, v in zip(("wFresh", "wImportance", "wMood", "wEvidence"), vals):
        cfg[k] = round(v / total, 2)


def read_mem_cfg():
    cfg = dict(_MEM_DEFAULTS)
    cfg.update({k: v for k, v in ms._load_json_file(MEM_CFG_FILE, {}).items() if k in _MEM_DEFAULTS})
    for k, (lo, hi) in _MEM_NUM_INT.items():
        try:
            cfg[k] = int(cfg[k])
        except (TypeError, ValueError):
            cfg[k] = _MEM_DEFAULTS[k]
        cfg[k] = max(lo, min(hi, cfg[k]))
    try:
        cfg["eviMin"] = max(0.0, min(0.9, float(cfg["eviMin"])))
    except (TypeError, ValueError):
        cfg["eviMin"] = _MEM_DEFAULTS["eviMin"]
    _mem_norm_weights(cfg)
    cfg["enabled"] = cfg["enabled"] is not False     # 只有显式 false 才关，等级同 TS 端
    cfg["autoRecall"] = cfg["autoRecall"] is not False
    return cfg


def _mem_load_file(session_id):
    """读某会话的记库（与 index.ts loadMemFile 同结构：{entries:[最新在前], days:{}}）。"""
    safe = re.sub(r"[^a-zA-Z0-9_-]", "_", session_id)
    path = MEM_DIR / (safe + ".json")
    raw = ms._load_json_file(path, {})
    if isinstance(raw, dict):
        entries = raw.get("entries")
        if not isinstance(entries, list):
            entries = []
        days = raw.get("days")
        if not isinstance(days, dict):
            days = {}
        return entries, days
    return [], {}


def _mem_save_file(session_id, entries, days):
    """写某会话的记库：改前备份 .bak + 原子替换（照 TS 端 saveMemFile 的语义）。"""
    safe = re.sub(r"[^a-zA-Z0-9_-]", "_", session_id)
    MEM_DIR.mkdir(parents=True, exist_ok=True)
    path = MEM_DIR / (safe + ".json")
    if path.exists():
        shutil.copyfile(str(path), str(path) + ".bak")
    tmp = str(path) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"entries": entries[:500], "days": days}, f, ensure_ascii=False, indent=2)
    os.replace(tmp, str(path))


def _mem_cache_bj_time():
    return datetime.now(_BJ).strftime("%Y-%m-%d %H:%M:%S")


async def api_state(_):
    cfg = _read_cfg()
    st = ms._load_json_file(ms.STATE_FILE, {"seen": [], "days": {}, "lastScanAt": ""})
    today = _bj_now().strftime("%Y-%m-%d")
    reports = []
    if ms.OUT_DIR.exists():
        for p in sorted(ms.OUT_DIR.glob("*.md"), key=lambda x: x.name, reverse=True):
            reports.append({"date": p.stem, "size": p.stat().st_size})
    su = ms._load_json_file(SU_LOG_FILE, {})   # {state: {...}, log: [...]}
    su_state = su.get("state", {}) if isinstance(su.get("state", {}), dict) else {}
    # memory 记忆：全库汇总（跳过 .bak/.tmp），最新在前
    mem_latest, mem_total, mem_files_n = [], 0, 0
    if MEM_DIR.exists():
        for p in sorted(MEM_DIR.glob("*.json")):
            if p.name.endswith(".tmp") or p.name.endswith(".bak"):
                continue
            mem_files_n += 1
            data = ms._load_json_file(p, {})
            entries = data.get("entries") if isinstance(data.get("entries"), list) else []
            mem_total += len(entries)
            mem_latest.extend(e for e in entries if isinstance(e, dict) and not e.get("supersededBy"))
    mem_latest.sort(key=lambda e: int(e.get("ts") or 0), reverse=True)
    mem_log = ms._load_json_file(MEM_LOG_FILE, {})
    return web.json_response({
        "loggedIn": bool(ms.load_credential()),
        "today": today,
        "roundsToday": int(st.get("days", {}).get(today, 0)),
        "perDay": cfg["perDay"],
        "lastScanAt": st.get("lastScanAt", ""),
        "seenCount": len(st.get("seen", [])),
        "inValley": in_valley(),
        "reports": reports,
        "config": cfg,
        "account": ms._load_account_state(),
        "speakUp": {
            "state": {
                "lastRollAt": int(su_state.get("lastRollAt") or 0),
                "lastSpeakAt": int(su_state.get("lastSpeakAt") or 0),
                "misses": int(su_state.get("misses") or 0),
                "sessionID": str(su_state.get("sessionID") or ""),
            },
            "log": list(su.get("log") or [])[:12],
            "cfg": read_su_cfg(),
        },
        "memory": {
            "cfg": read_mem_cfg(),
            "total": mem_total,
            "files": mem_files_n,
            "latest": mem_latest[:12],
            "recalls": list(mem_log.get("recalls") or [])[:6],
            "writes": list(mem_log.get("writes") or [])[:6],
        },
    })


async def api_report(request):
    date = request.match_info["date"]
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):  # YYYY-MM-DD，防目录穿越
        return web.json_response({"error": "bad date"}, status=422)
    target = ms.OUT_DIR / (date + ".md")
    if not str(target.resolve()).startswith(str(ms.OUT_DIR.resolve())):
        return web.json_response({"error": "bad date"}, status=422)
    if not target.exists():
        return web.Response(text="该日期没有报告", content_type="text/plain", status=404)
    return web.Response(text=target.read_text(encoding="utf-8"), content_type="text/plain")


async def api_config(request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    cfg = _read_cfg()
    mode = body.get("autoMode")
    if mode in ("manual", "valley", "always"):
        cfg["autoMode"] = mode
    dm = body.get("dmMode")
    if dm in ("off", "only", "both"):
        cfg["dmMode"] = dm
    for k in ("perDay", "perScan", "gapMin", "minViews", "minAgeDays"):
        if k in body and isinstance(body[k], (int, float)):
            cfg[k] = int(body[k])
    if "defaultDirection" in body and str(body["defaultDirection"]).strip():
        cfg["defaultDirection"] = str(body["defaultDirection"]).strip()
    cfg["perDay"] = max(0, min(100, cfg["perDay"]))
    cfg["perScan"] = max(1, min(10, cfg["perScan"]))
    cfg["gapMin"] = max(5, min(720, cfg["gapMin"]))
    cfg["minViews"] = max(0, cfg["minViews"])
    cfg["minAgeDays"] = max(0, cfg["minAgeDays"])
    ms._save_json_atomic(ms.CONFIG_FILE, cfg)
    print(f"[panel] 配置已保存：autoMode={cfg['autoMode']} perDay={cfg['perDay']}", file=sys.stderr)
    return web.json_response({"success": True, "config": cfg})


async def api_scan(request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    cfg = _read_cfg()
    direction = str(body.get("direction") or "").strip() or cfg["defaultDirection"]
    count = int(body.get("count") or cfg["perScan"])
    count = max(1, min(count, 10))
    if ms._scan_lock.locked():
        return web.json_response({"success": False, "error": "已有扫描在跑，稍候"}, status=409)
    asyncio.ensure_future(ms._do_scan(direction, count, -1, -1))
    print(f"[panel] 手动扫描已触发：{direction} x{count}", file=sys.stderr)
    return web.json_response({"success": True, "direction": direction, "count": count})


async def api_speakup(request):
    """保存主动搭话配置：全字段白名单 + clamp，与 index.ts 的 readSpeakUpCfg 同口径。"""
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    cfg = read_su_cfg()
    cfg["enabled"] = body.get("enabled") is True
    cfg["dryRun"] = body.get("dryRun") is not False
    for k, (lo, hi) in _SU_NUM.items():
        if k in body and isinstance(body[k], (int, float)):
            cfg[k] = int(body[k])
        cfg[k] = max(lo, min(hi, cfg[k]))
    if "prompt" in body and str(body.get("prompt") or "").strip():
        cfg["prompt"] = str(body["prompt"]).strip()
    # targetSessionID 不经面板改（高级项，直接编辑 speakup_config.json），保持原值
    ms._save_json_atomic(SU_CFG_FILE, cfg)
    print(f"[panel] 主动搭话已保存：enabled={cfg['enabled']} dryRun={cfg['dryRun']} intervalMin={cfg['intervalMin']}", file=sys.stderr)
    return web.json_response({"success": True, "config": cfg})


async def api_memory(request):
    """保存 memory 记忆配置：白名单 + clamp + 权重归一，与 index.ts readMemCfg 同口径。"""
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    cfg = read_mem_cfg()
    cfg["enabled"] = body.get("enabled") is True
    cfg["autoRecall"] = body.get("autoRecall") is not False
    for k, (lo, hi) in _MEM_NUM_INT.items():
        if k in body and isinstance(body[k], (int, float)):
            cfg[k] = int(body[k])
        cfg[k] = max(lo, min(hi, cfg[k]))
    if "eviMin" in body and isinstance(body["eviMin"], (int, float)):
        cfg["eviMin"] = max(0.0, min(0.9, float(body["eviMin"])))
    for k in ("wFresh", "wImportance", "wMood", "wEvidence"):
        if k in body and isinstance(body[k], (int, float)):
            cfg[k] = float(body[k])
    _mem_norm_weights(cfg)
    ms._save_json_atomic(MEM_CFG_FILE, cfg)
    print(f"[panel] memory 记忆已保存：enabled={cfg['enabled']} halfLife={cfg['halfLifeDays']}d eviMin={cfg['eviMin']}", file=sys.stderr)
    return web.json_response({"success": True, "config": cfg})


_SCAN_TXT = {".md", ".markdown", ".txt", ".text", ".log", ".rst", ".org"}

async def api_memory_scan(request):
    """扫旧档案出候选行（往下两层，md/txt 系；与原版认得的格式一致）。"""
    try:
        body = await request.json()
    except Exception:
        body = {}
    path = str((body or {}).get("path") or "").strip().strip('"')
    if not path:
        return web.json_response({"success": False, "error": "缺 path"}, status=422)
    base = Path(os.path.expanduser(path))
    if not base.exists():
        return web.json_response({"success": False, "error": "路径不存在"}, status=404)
    files = []
    if base.is_file():
        files = [base]
    else:
        for child in base.iterdir():
            if child.is_file() and child.suffix.lower() in _SCAN_TXT:
                files.append(child)
            elif child.is_dir():
                for g in child.iterdir():
                    if g.is_file() and g.suffix.lower() in _SCAN_TXT:
                        files.append(g)
    files = files[:40]
    seen, candidates = set(), []
    for f in files:
        try:
            if f.stat().st_size > 5 * 1024 * 1024:
                continue
            text = f.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for line in text.splitlines():
            s = re.sub(r"^[#>\-\*\s•·]+", "", line.strip()).strip()
            if not (8 <= len(s) <= 120) or s in seen:
                continue
            if s.startswith(("http", "|")) or "```" in s or "https:" in s or "http:" in s:
                continue
            seen.add(s)
            candidates.append(s)
            if len(candidates) >= 200:
                break
        if len(candidates) >= 200:
            break
    return web.json_response({"success": True, "files": len(files), "count": len(candidates), "candidates": candidates})


async def api_memory_import(request):
    """把勾选的候选行直入茶话会话的记库（轻量直入：kind=event、重要度 2、检索词本地 2-gram；不占每日配额）。"""
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    texts = [t.strip() for t in (body.get("texts") or []) if isinstance(t, str) and t.strip()][:100]
    if not texts:
        return web.json_response({"success": False, "error": "没勾选任何条目"}, status=422)
    # 导入目标：茶话会话（主动搭话掷中后自动建的那个），面板无法预知 ID，从 speakup 状态里拿
    su_state = (ms._load_json_file(SU_LOG_FILE, {}).get("state") or {})
    fid = str(su_state.get("sessionID") or "").strip()
    if not fid:
        return web.json_response({"success": False, "error": "茶话会话还没建立（让主动搭话掷中一次就自动创建）"}, status=409)
    entries, days = _mem_load_file(fid)
    imported, skipped = 0, 0
    now_ms = int(_bj_now().timestamp() * 1000)
    fresh = []
    existing_head = {str(e.get("text", ""))[:24] for e in entries[:60]}
    for t in texts:
        if t[:24] in existing_head:
            skipped += 1
            continue
        now_ms += 1
        fresh.append({
            "id": "p" + format(now_ms, "x") + format(imported, "x"),
            "ts": now_ms,
            "at": _mem_cache_bj_time(),
            "text": t,
            "kind": "event",
            "importance": 2,
            "mood": 0,
            "keys": [],
            "words": mem_tokens(t),
        })
        existing_head.add(t[:24])
        imported += 1
    if imported:
        _mem_save_file(fid, fresh + entries, days)
        # 写入流水（父端只读展示）
        log = ms._load_json_file(MEM_LOG_FILE, {})
        writes = log.get("writes") if isinstance(log.get("writes"), list) else []
        writes.insert(0, {"at": _mem_cache_bj_time(), "type": "import", "sessionID": fid, "imported": imported, "skipped": skipped})
        ms._save_json_atomic(MEM_LOG_FILE, {"recalls": log.get("recalls") or [], "writes": writes[:12]})
    return web.json_response({"success": True, "imported": imported, "skipped": skipped, "sessionID": fid})


async def index(_):
    return web.Response(text=_HTML, content_type="text/html")


def main():
    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_get("/api/state", api_state)
    app.router.add_get("/api/report/{date}", api_report)
    app.router.add_post("/api/config", api_config)
    app.router.add_post("/api/scan", api_scan)
    app.router.add_post("/api/speakup", api_speakup)
    app.router.add_post("/api/memory", api_memory)
    app.router.add_post("/api/memory_scan", api_memory_scan)
    app.router.add_post("/api/memory_import", api_memory_import)
    print(f"[panel] http://127.0.0.1:{PORT}", file=sys.stderr)
    web.run_app(app, host="127.0.0.1", port=PORT, print=None)


if __name__ == "__main__":
    main()
