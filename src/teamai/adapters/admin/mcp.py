"""MCP server 管理路由。

配置单位是频道（对齐 policy/预算）。凭据（headers）**不出现在任何响应里**：
``mcp_server_to_dict`` 一律脱敏回显 ``***``；更新时传 ``***`` 表示保留原值、
传空串表示删除该键、其余覆盖。保存本身不做连接验证 —— 配置前先用
``POST /mcp-servers/test`` 握手一次，把验证放在用户主动触发的位置。
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, HTTPException

from teamai.adapters.admin.serializers import HEADER_PLACEHOLDER, mcp_server_to_dict
from teamai.container import Container
from teamai.domain.identity import gen_id
from teamai.domain.models import McpServer
from teamai.domain.models.mcp import NAME_PATTERN
from teamai.domain.ports.mcp import McpConnectionError

_NAME_RE = re.compile(NAME_PATTERN)


def _validate(name: str, url: str) -> None:
    """name 与 url 的格式校验。两者都进工具名前缀 / 网络层，需在入库前拦下。"""
    if not _NAME_RE.fullmatch(name):
        raise HTTPException(
            status_code=422,
            detail="name 只允许小写字母、数字与连字符（会拼进工具名前缀 mcp__<name>__）",
        )
    if not url.startswith(("http://", "https://")):
        raise HTTPException(status_code=422, detail="url 必须是 http(s) 端点")


def _merge_headers(original: dict[str, str], submitted: dict[str, str]) -> dict[str, str]:
    """合并前端提交的 headers（脱敏回显的配套语义）：
    - 值等于 ``***``（占位）→ 保留原值
    - 值等于空串 → 删除该键
    - 其他 → 覆盖
    """
    merged = dict(original)
    for key, value in submitted.items():
        if value == HEADER_PLACEHOLDER:
            continue
        if value == "":
            merged.pop(key, None)
        else:
            merged[key] = value
    return merged


def build_mcp_router(container: Container) -> APIRouter:
    router = APIRouter()

    @router.get("/channels/{channel_instance_id}/mcp-servers")
    async def list_servers(channel_instance_id: str) -> list[dict[str, Any]]:
        servers = await container.mcp_repo.list_for_channel(channel_instance_id)
        return [mcp_server_to_dict(s) for s in servers]

    @router.post("/channels/{channel_instance_id}/mcp-servers")
    async def create_server(channel_instance_id: str, body: dict[str, Any]) -> dict[str, Any]:
        name = body.get("name", "")
        url = body.get("url", "")
        _validate(name, url)
        if await container.mcp_repo.find_by_name(channel_instance_id, name):
            raise HTTPException(status_code=409, detail="该频道已存在同名 MCP server")

        server = McpServer(
            id=gen_id("mcp"),
            channel_instance_id=channel_instance_id,
            name=name,
            url=url,
            headers=body.get("headers") or {},
            enabled=bool(body.get("enabled", True)),
        )
        # 事务边界由用例层声明（仓储只 flush 不 commit，见
        # tests/unit/test_repository_commit.py 的约束）
        async with container.uow:
            await container.mcp_repo.upsert(server)
        return mcp_server_to_dict(server)

    @router.put("/channels/{channel_instance_id}/mcp-servers/{server_id}")
    async def update_server(
        channel_instance_id: str, server_id: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        server = await container.mcp_repo.get(channel_instance_id, server_id)
        if server is None:
            raise HTTPException(status_code=404, detail="MCP server 不存在")

        if "name" in body and body["name"] != server.name:
            _validate(body["name"], server.url)
            if await container.mcp_repo.find_by_name(channel_instance_id, body["name"]):
                raise HTTPException(status_code=409, detail="该频道已存在同名 MCP server")
            server.name = body["name"]
        if "url" in body:
            _validate(server.name, body["url"])
            server.url = body["url"]
        if "headers" in body:
            server.headers = _merge_headers(server.headers, body["headers"])
        if "enabled" in body:
            server.enabled = bool(body["enabled"])

        server.updated_at = datetime.now(UTC)
        async with container.uow:
            await container.mcp_repo.upsert(server)
        return mcp_server_to_dict(server)

    @router.delete("/channels/{channel_instance_id}/mcp-servers/{server_id}")
    async def delete_server(channel_instance_id: str, server_id: str) -> dict[str, Any]:
        server = await container.mcp_repo.get(channel_instance_id, server_id)
        if server is None:
            raise HTTPException(status_code=404, detail="MCP server 不存在")
        async with container.uow:
            await container.mcp_repo.delete(channel_instance_id, server_id)
        return {"ok": True}

    @router.post("/channels/{channel_instance_id}/mcp-servers/test")
    async def test_server(channel_instance_id: str, body: dict[str, Any]) -> dict[str, Any]:
        url = body.get("url", "")
        if not url.startswith(("http://", "https://")):
            raise HTTPException(status_code=422, detail="url 必须是 http(s) 端点")
        try:
            tools = await container.mcp.test_connection(url, body.get("headers") or {})
        except McpConnectionError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"tools": tools}

    return router
