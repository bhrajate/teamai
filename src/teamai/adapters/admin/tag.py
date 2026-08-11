"""标签模板路由。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

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
        name = str(body.get("name", "")).strip()
        if not name:
            raise HTTPException(status_code=400, detail="name 不能为空")
        instruction = str(body.get("instruction", "")).strip()
        if not instruction:
            # 没有指令的标签激活后什么也不会改变，等于一个空壳
            raise HTTPException(status_code=400, detail="instruction 不能为空")

        tag = await container.tags.create(
            channel_instance_id,
            name,
            instruction,
            role=body.get("role"),
            output_style=body.get("output_style"),
            created_by=body.get("created_by"),
        )
        return tag_to_dict(tag)

    @router.patch("/channels/{channel_instance_id}/tags/{tag_id}")
    async def set_tag_active(
        channel_instance_id: str, tag_id: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        """激活/停用标签。

        channel_instance_id 入路径而非只取 tag_id：标签按频道隔离，
        路径里带上频道才能在改之前确认这个标签确实属于该频道 ——
        否则拿到任一 tag_id 就能改别的频道的标签。
        """
        active = body.get("active")
        if not isinstance(active, bool):
            raise HTTPException(status_code=400, detail="active 必须为布尔值")

        tags = await container.tags.list(channel_instance_id)
        target = next((t for t in tags if t.id == tag_id), None)
        if target is None:
            raise HTTPException(status_code=404, detail="该频道下没有这个标签")

        await container.tags.set_active(tag_id, active)
        target.active = active
        return tag_to_dict(target)

    @router.delete("/channels/{channel_instance_id}/tags/{tag_id}")
    async def delete_tag(
        channel_instance_id: str, tag_id: str, actor: str | None = None
    ) -> dict[str, str]:
        tags = await container.tags.list(channel_instance_id)
        if not any(t.id == tag_id for t in tags):
            raise HTTPException(status_code=404, detail="该频道下没有这个标签")

        await container.tags.delete(channel_instance_id, tag_id, actor=actor)
        return {"status": "deleted"}

    return router
