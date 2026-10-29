# bili_scan 命令行驱动：python bili_scan_cli.py [方向] [条数]
# 等价于 MCP 工具 bili_scan（同一个 _do_scan 编排函数，不走 MCP 框架）
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "vendor" / "bilibili-mcp"))
import mcp_server as ms  # noqa: E402

direction = sys.argv[1] if len(sys.argv) > 1 else ""
count = int(sys.argv[2]) if len(sys.argv) > 2 else 3
result = asyncio.run(ms._do_scan(direction, count, -1, -1))
print(json.dumps(result, ensure_ascii=False))
