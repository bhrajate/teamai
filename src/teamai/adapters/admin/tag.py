"""标签模板路由。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from teamai.adapters.admin.serializers import tag_to_dict
from teamai.container import Container


def build_tag_router(container: Container) -> APIRouter:
    router = APIRouter()

    @router.get("/channels/{channel_instance_id}/tags")
    async def list_tags(channel_instance_id: str) -> list[dict[str, Any]]:
        tags = await container.tags.list(channel_instance_id)
        return [tag_to_dict(t) for t in tags]

    @router.post("/channels/{channel_instance_id}/tags")
    async def create_tag(channel_instance_id: str, body: dict[str, Any]) -> dict[str, Any]:
        tag = await container.tags.create(
            channel_instance_id,
            body.get("name", ""),
            body.get("instruction", ""),
            role=body.get("role"),
            output_style=body.get("output_style"),
            created_by=body.get("created_by"),
        )
        return tag_to_dict(tag)

    return router
