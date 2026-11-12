"""阶段 F：MCP 薄适配层（把 FairMeet 的 agent 接口暴露为 MCP 工具）。"""

from fairmeet.mcp.server import AgentHttpClient, MCPServer, ToolError

__all__ = ["AgentHttpClient", "MCPServer", "ToolError"]
