"""审计查询路由。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from teamai.adapters.admin.serializers import audit_to_dict
from teamai.container import Container


def build_audit_router(container: Container) -> APIRouter:
    router = APIRouter()

    @router.get("/channels/{channel_instance_id}/audit")
    async def list_audit(channel_instance_id: str, limit: int = 100) -> list[dict[str, Any]]:
        logs = await container.audit_repo.list_by_channel(channel_instance_id, limit=limit)
        return [audit_to_dict(log) for log in logs]

    return router
