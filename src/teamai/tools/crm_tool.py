"""CRM 工具连接器（Salesforce 风格数据查询）。预留集成点。"""

from __future__ import annotations

import os
from typing import Any

from teamai.tools.base import BaseTool, ToolError, ToolResult


class CRMQueryTool(BaseTool):
    name = "crm"
    description = "查询 CRM 客户/工单数据（对接 Salesforce 类服务）。"
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["query"]},
            "object": {"type": "string", "description": "查询对象，如 Account/Case"},
            "fields": {"type": "array", "items": {"type": "string"}},
            "filter": {"type": "string", "description": "SOQL 风格过滤条件"},
        },
        "required": ["action"],
    }

    def __init__(self, instance_url: str | None = None, token: str | None = None) -> None:
        self._instance_url = instance_url or os.environ.get("CRM_INSTANCE_URL", "")
        self._token = token or os.environ.get("CRM_TOKEN", "")

    async def call(self, args: dict[str, Any], auth_scope: object) -> ToolResult:
        if not self._instance_url or not self._token:
            raise ToolError("CRM 服务未配置（CRM_INSTANCE_URL / CRM_TOKEN）")
        raise ToolError("CRM 查询需要具体数据源实现")


def build_crm_tool() -> CRMQueryTool:
    return CRMQueryTool()
