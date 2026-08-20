"""审计查询路由。

两个作用域：按频道，与全局。后者是不隶属任何频道的变更（目前只有 skill 库 ——
它是全局定义、按频道启用的，改正文这个动作没有频道可归属）。

全局流水给一个专门端点而非让前端去打 ``/channels/global/audit``：
``GLOBAL_SCOPE`` 这个取值约定是后端的事，泄到前端就成了两处各存一份，
而 web/src/api/types.ts 顶部那段注释说的正是这类跨栈对齐只能靠人守。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from teamai.adapters.admin.serializers import audit_to_dict
from teamai.container import Container
from teamai.domain.models import GLOBAL_SCOPE


def build_audit_router(container: Container) -> APIRouter:
    router = APIRouter()

    @router.get("/channels/{channel_instance_id}/audit")
    async def list_audit(channel_instance_id: str, limit: int = 100) -> list[dict[str, Any]]:
        logs = await container.audit_repo.list_by_channel(channel_instance_id, limit=limit)
        return [audit_to_dict(log) for log in logs]

    @router.get("/audit/global")
    async def list_global_audit(limit: int = 100) -> list[dict[str, Any]]:
        """全局资源的变更流水（skill 库的增删改）。

        与按频道那条共用 ``list_by_channel`` —— GLOBAL_SCOPE 就是存在
        channel_instance_id 列里的一个取值，不需要额外的仓储方法。
        """
        logs = await container.audit_repo.list_by_channel(GLOBAL_SCOPE, limit=limit)
        return [audit_to_dict(log) for log in logs]

    return router
