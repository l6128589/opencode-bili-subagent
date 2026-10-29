# 用 CDP 从用户已登录的 Chrome 里导出 B 站 Cookie（HttpOnly 也能取）
# 前置：Chrome 以 --remote-debugging-port=9222 启动，并且浏览器里已登录 bilibili.com
# 做法：连 ws（aiohttp，mcp 的依赖已有） → Network.getAllCookies → 筛 bilibili 域 →
#       原子写 bili_credential.json（sessdata/bili_jct/buvid3/dedeuserid）
import asyncio
import json
import os
import sys
from pathlib import Path

import aiohttp

CRED_FILE = Path(__file__).parent / "vendor" / "bilibili-mcp" / "bili_credential.json"
DEVTOOLS = "http://127.0.0.1:9222"
CMD_ID = 1


async def cdp_call(ws, method, params=None):
    global CMD_ID
    await ws.send_json({"id": CMD_ID, "method": method, "params": params or {}})
    CMD_ID += 1
    while True:
        msg = json.loads(await ws.receive_str())
        if msg.get("id") == CMD_ID - 1:
            return msg


async def main():
    async with aiohttp.ClientSession() as http:
        # 1) 找一个 B 站标签页（没有就在新标签页里打开 bilibili.com）
        tabs = await (await http.get(f"{DEVTOOLS}/json", timeout=aiohttp.ClientTimeout(total=5))).json()
        target = next((t for t in tabs if "bilibili.com" in t.get("url", "") and t["type"] == "page"), None)
        if not target:
            fresh = await http.put(f"{DEVTOOLS}/json/new?https://www.bilibili.com", timeout=aiohttp.ClientTimeout(total=5))
            target = (await fresh.json()) if fresh.status == 200 else None
        target_id = target["id"]

        # 2) 连该标签页的 ws，调 Network.getAllCookies
        ws_url = target["webSocketDebuggerUrl"]
        async with http.ws_connect(ws_url, timeout=10) as ws:
            resp = await cdp_call(ws, "Network.enable")
            resp = await cdp_call(ws, "Network.getAllCookies")
            if "error" in resp:
                print(f"[cds] Network.getAllCookies 失败: {resp['error']}", file=sys.stderr)
                sys.exit(1)
            cookies = resp["result"]["cookies"]

    # 3) 筛 B 站域，抽四件套
    bili = [c for c in cookies if "bilibili.com" in c.get("domain", "")]
    wanted = {c["name"]: c for c in bili}

    def pick(name):
        c = wanted.get(name)
        return (c.get("value", "") if c else "")

    sessdata = pick("SESSDATA")
    bili_jct = pick("bili_jct")
    dedeuserid = pick("DedeUserID") or pick("dedeuserid")
    buvid3 = pick("buvid3")

    missing = [n for n, v in [("SESSDATA", sessdata), ("bili_jct", bili_jct), ("DedeUserID", dedeuserid)] if not v]
    if missing:
        print("[cds] 缺字段: " + ", ".join(missing) + " —— Chrome 里要已登录 bilibili.com 才有", file=sys.stderr)
        print(f"[cds] 本次到手 {len(bili)} 条 bili cookie：{sorted(wanted)[:20]}", file=sys.stderr)
        sys.exit(2)

    cred = {"sessdata": sessdata, "bili_jct": bili_jct, "buvid3": buvid3, "dedeuserid": dedeuserid}
    tmp = str(CRED_FILE) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cred, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CRED_FILE)
    print("[cds] 凭证已写入 " + str(CRED_FILE))
    print(f"[cds] DedeUserID={dedeuserid[:4]}***  SESSDATA长度={len(sessdata)}  bili_jct长度={len(bili_jct)}")


asyncio.run(main())
