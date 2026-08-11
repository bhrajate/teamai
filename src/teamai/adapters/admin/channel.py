"""频道实例路由。

这组是 Admin 控制台的入口：其余所有资源端点都以 `channel_instance_id` 为路径
参数，没有本模块前端只能让人手输 ID。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

from teamai.adapters.admin.serializers import channel_to_dict
from teamai.container import Container


def build_channel_router(container: Container) -> APIRouter:
    router = APIRouter()

    @router.get("/channels")
    async def list_channels() -> list[dict[str, Any]]:
        return [channel_to_dict(c) for c in await container.channels.list()]

    @router.get("/channels/{channel_instance_id}")
    async def get_channel(channel_instance_id: str) -> dict[str, Any]:
        instance = await container.channels.get(channel_instance_id)
        if instance is None:
            raise HTTPException(status_code=404, detail="频道实例不存在")
        return channel_to_dict(instance)

    @router.patch("/channels/{channel_instance_id}")
    async def update_channel(channel_instance_id: str, body: dict[str, Any]) -> dict[str, Any]:
        """改频道开关。字段缺省即不动，故可单开关提交。"""
        instance = await container.channels.update_settings(
            channel_instance_id,
            ambient_enabled=body.get("ambient_enabled"),
            cross_channel_learning=body.get("cross_channel_learning"),
            actor=body.get("actor"),
        )
        if instance is None:
            raise HTTPException(status_code=404, detail="频道实例不存在")
        return channel_to_dict(instance)

    return router
