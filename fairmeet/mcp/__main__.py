"""以 stdio 方式启动 FairMeet MCP 服务。

用法（在 MCP 客户端配置中）：
    command: python
    args: ["-m", "fairmeet.mcp"]
    env:
      FAIRMEET_BASE_URL: http://127.0.0.1:8000
      FAIRMEET_AGENT_TOKEN: fm_agent_...
"""

from __future__ import annotations

import os
import sys

from fairmeet.mcp.server import AgentHttpClient, MCPServer


def main() -> int:
    token = os.getenv("FAIRMEET_AGENT_TOKEN", "").strip()
    if not token:
        print(
            "FAIRMEET_AGENT_TOKEN 未设置：请先在旅程中创建 agent 委托，并把令牌放入环境变量。",
            file=sys.stderr,
        )
        return 2
    base_url = os.getenv("FAIRMEET_BASE_URL", "http://127.0.0.1:8000").strip()
    MCPServer(AgentHttpClient(base_url, token)).serve()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
