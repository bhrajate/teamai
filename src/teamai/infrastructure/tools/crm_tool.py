"""CRM 工具连接器（Salesforce 风格数据查询）。预留集成点。"""

from __future__ import annotations

import os
from typing import Literal

from pydantic_ai import Tool

from teamai.infrastructure.tools.base import fail


def build_crm_tool(instance_url: str | None = None, token: str | None = None) -> Tool:
    resolved_url = instance_url or os.environ.get("CRM_INSTANCE_URL", "")
    resolved_token = token or os.environ.get("CRM_TOKEN", "")

    async def crm(
        action: Literal["query"],
        object: str | None = None,
        fields: list[str] | None = None,
        filter: str | None = None,
    ) -> str:
        """查询 CRM 客户/工单数据（对接 Salesforce 类服务）。

        Args:
            action: 要执行的操作，当前仅支持 query。
            object: 查询对象，如 Account 或 Case。
            fields: 需要返回的字段列表。
            filter: SOQL 风格过滤条件。
        """
        if not resolved_url or not resolved_token:
            return fail("CRM 服务未配置（CRM_INSTANCE_URL / CRM_TOKEN）")
        return fail("CRM 查询需要具体数据源实现")

    return Tool(crm)
