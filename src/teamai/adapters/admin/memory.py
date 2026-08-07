"""记忆管理路由。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

from teamai.adapters.admin.serializers import memory_to_dict
from teamai.container import Container


def build_memory_router(container: Container) -> APIRouter:
    router = APIRouter()

    @router.get("/channels/{channel_instance_id}/memories")
    async def list_memories(channel_instance_id: str) -> list[dict[str, Any]]:
        entries = await container.memory.list(channel_instance_id)
        return [memory_to_dict(e) for e in entries]

    @router.post("/channels/{channel_instance_id}/memories")
    async def create_memory(channel_instance_id: str, body: dict[str, Any]) -> dict[str, Any]:
        content = body.get("content", "")
        if not content:
            raise HTTPException(status_code=400, detail="content 不能为空")
        entry = await container.memory.store(channel_instance_id, content, source_user_id=body.get("user_id"))
        return memory_to_dict(entry)

    @router.delete("/memories/{entry_id}")
    async def delete_memory(entry_id: str, actor: str | None = None) -> dict[str, str]:
        await container.memory.delete(entry_id, actor=actor)
        return {"status": "deleted"}

    return router
