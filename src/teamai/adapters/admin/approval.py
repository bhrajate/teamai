"""待审批列表路由。**只读**。

有意不给放行端点：Admin API 只有一个共享令牌，``actor`` 是前端随便填的字符串，
而审批的审计链不该建立在不可信字段上。放行必须回到频道线程里做 —— 那里的
``user_id`` 是平台签过名的。

所以这里的用途是「能看见、能追溯、能看到参数全文」：控制台上看到有东西在等批，
然后去线程里 ``/approve``。完整理由见 docs/SPEC-hitl-approval.md §6.4。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from teamai.adapters.admin.serializers import pending_approval_to_dict
from teamai.container import Container
from teamai.domain.models import TaskStatus


def build_approval_router(container: Container) -> APIRouter:
    router = APIRouter()

    @router.get("/channels/{channel_instance_id}/approvals")
    async def list_pending(channel_instance_id: str) -> list[dict[str, Any]]:
        """该频道所有等待审批的操作。

        驱动方式是「先查 WAITING_INPUT 的任务，再按 task_id 取待批项」而不是
        反过来：待批项的仓储按 task_id 点查，没有「按频道列出」的方法 —— 而加
        那个方法就要在 task_checkpoints 上加 channel_instance_id 列（冗余，且
        与任务表不一致时无法判断谁对）。任务表本来就带频道。
        """
        tasks = await container.task_repo.list_by_channel(
            channel_instance_id, TaskStatus.WAITING_INPUT
        )
        out: list[dict[str, Any]] = []
        for task in tasks:
            pending = await container.checkpoint_repo.get_pending_approval(task.id)
            if pending is None:
                # WAITING_INPUT 但没有待批项：理论上不该出现（该状态目前只由
                # 审批产生），但不该因此 500 —— 跳过并让列表照常返回。
                continue
            out.append(pending_approval_to_dict(pending, task))
        return out

    return router
