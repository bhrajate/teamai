"""监控工具连接器（Datadog/Sentry 风格告警与指标查询）。

预留集成点：未配置端点时返回配置错误，避免误报。
"""

from __future__ import annotations

import os
from typing import Literal

from pydantic_ai import Tool

from teamai.infrastructure.tools.base import fail, ok


def build_monitoring_tool(endpoint: str | None = None, api_key: str | None = None) -> Tool:
    resolved_endpoint = endpoint or os.environ.get("MONITORING_ENDPOINT", "")
    resolved_key = api_key or os.environ.get("MONITORING_API_KEY", "")

    async def monitoring(
        action: Literal["alerts", "metric"],
        scope: str | None = None,
        metric_name: str | None = None,
    ) -> str:
        """查询监控告警与系统指标（对接 Datadog/Sentry 类服务）。

        Args:
            action: 要执行的操作，取 alerts（查告警）或 metric（查指标）。
            scope: 查询范围，如服务名或团队名。
            metric_name: 指标名，action=metric 时必填。
        """
        if not resolved_endpoint or not resolved_key:
            return fail("监控服务未配置（MONITORING_ENDPOINT / MONITORING_API_KEY）")
        if action == "alerts":
            return ok(alerts=[], scope=scope, note="监控端点已配置，等待实现具体告警查询")
        return fail("指标查询需要具体数据源实现")

    return Tool(monitoring)
