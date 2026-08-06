"""监控工具连接器（Datadog/Sentry 风格告警与指标查询）。

预留集成点：未配置端点时返回配置错误，避免误报。
"""

from __future__ import annotations

import os
from typing import Any

from teamai.tools.base import BaseTool, ToolError, ToolResult


class MonitoringTool(BaseTool):
    name = "monitoring"
    description = "查询监控告警与系统指标（对接 Datadog/Sentry 类服务）。"
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["alerts", "metric"]},
            "scope": {"type": "string", "description": "查询范围，如服务名/团队名"},
            "metric_name": {"type": "string", "description": "指标名（metric 使用）"},
        },
        "required": ["action"],
    }

    def __init__(self, endpoint: str | None = None, api_key: str | None = None) -> None:
        self._endpoint = endpoint or os.environ.get("MONITORING_ENDPOINT", "")
        self._api_key = api_key or os.environ.get("MONITORING_API_KEY", "")

    async def call(self, args: dict[str, Any], auth_scope: object) -> ToolResult:
        if not self._endpoint or not self._api_key:
            raise ToolError("监控服务未配置（MONITORING_ENDPOINT / MONITORING_API_KEY）")
        action = args.get("action")
        if action == "alerts":
            return ToolResult(ok=True, data={"alerts": [], "note": "监控端点已配置，等待实现具体告警查询"})
        if action == "metric":
            raise ToolError("指标查询需要具体数据源实现")
        raise ToolError(f"不支持的监控操作: {action}")


def build_monitoring_tool() -> MonitoringTool:
    return MonitoringTool()
