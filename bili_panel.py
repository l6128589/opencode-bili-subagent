# -*- coding: utf-8 -*-
"""
bili-subagent 网页面板（零依赖：只用 venv 里已有的 aiohttp）
  状态卡（登录/额度/谷时/最近扫描）+ 配置表单（即改即生效）+ 手动扫描 + 报告阅读器
用法：.venv\Scripts\python.exe bili_panel.py   →  http://127.0.0.1:17178
注意：只监听 127.0.0.1，不对局域网开放。
"""
import asyncio
import re
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


async def api_state(_):
    cfg = _read_cfg()
    st = ms._load_json_file(ms.STATE_FILE, {"seen": [], "days": {}, "lastScanAt": ""})
    today = _bj_now().strftime("%Y-%m-%d")
    reports = []
    if ms.OUT_DIR.exists():
        for p in sorted(ms.OUT_DIR.glob("*.md"), key=lambda x: x.name, reverse=True):
            reports.append({"date": p.stem, "size": p.stat().st_size})
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


async def index(_):
    return web.Response(text=_HTML, content_type="text/html")


def main():
    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_get("/api/state", api_state)
    app.router.add_get("/api/report/{date}", api_report)
    app.router.add_post("/api/config", api_config)
    app.router.add_post("/api/scan", api_scan)
    print(f"[panel] http://127.0.0.1:{PORT}", file=sys.stderr)
    web.run_app(app, host="127.0.0.1", port=PORT, print=None)


if __name__ == "__main__":
    main()
