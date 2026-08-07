"""任务查询路由。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from teamai.adapters.admin.serializers import task_to_dict
from teamai.container import Container


def build_task_router(container: Container) -> APIRouter:
    router = APIRouter()

    @router.get("/channels/{channel_instance_id}/tasks")
    async def list_tasks(channel_instance_id: str) -> list[dict[str, Any]]:
        tasks = await container.orchestrator.list(channel_instance_id)
        return [task_to_dict(t) for t in tasks]

    return router
