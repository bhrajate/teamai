"""Agent 交互记录查询路由。

与审计路由的分工：审计答「发生了什么动作」，这里答「模型当时看到了什么、
回了什么、烧了多少 token」。排查一个错误回答时要的是后者。

只读，没有写入端点：这些记录由 AgentRuntime 在执行时产生，人工写入没有意义
（写进来的不是真实发生过的调用，会污染审计与成本统计）。清理走 worker 的
保留期巡检，不给手动删除入口 —— 审计类数据的删除应是策略性的、可审的，
而不是某个人点一下按钮。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from teamai.adapters.admin.serializers import interaction_to_dict
from teamai.container import Container

# 单页上限。这张表是全量增长的，控制台默认拉 50 条足够看最近情况；
# 放开到无上限会让频道用久之后的第一次打开就卡住。
MAX_LIMIT = 200


def build_interaction_router(container: Container) -> APIRouter:
    router = APIRouter()

    @router.get("/channels/{channel_instance_id}/interactions")
    async def list_interactions(
        channel_instance_id: str,
        limit: int = Query(default=50, ge=1, le=MAX_LIMIT),
    ) -> list[dict[str, Any]]:
        records = await container.interactions.list_by_channel(channel_instance_id, limit=limit)
        return [interaction_to_dict(r) for r in records]

    @router.get("/tasks/{task_id}/interactions")
    async def list_task_interactions(task_id: str) -> list[dict[str, Any]]:
        """某任务的完整往返，按时间正序（重试与多阶段任务会有多条）。"""
        records = await container.interactions.list_by_task(task_id)
        return [interaction_to_dict(r) for r in records]

    @router.get("/interactions/{interaction_id}")
    async def get_interaction(interaction_id: str) -> dict[str, Any]:
        record = await container.interaction_repo.get(interaction_id)
        if record is None:
            raise HTTPException(status_code=404, detail="交互记录不存在")
        return interaction_to_dict(record)

    return router
