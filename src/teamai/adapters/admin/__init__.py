"""Admin API：FastAPI 路由（频道/记忆/预算/策略/审计/任务/标签管理）。

按资源分模块，每个模块导出一个 build_*_router(container)，本文件负责挂上
`/api` 前缀并逐个 include。子路由自身不带前缀，完整路径写在各模块里。

⚠️ 新增资源模块后必须在 build_admin_router 里 include_router，否则该组路由
静默不注册。此约束由 tests/unit/test_admin_routes.py 校验。
"""

from __future__ import annotations

from fastapi import APIRouter

from teamai.adapters.admin.audit import build_audit_router
from teamai.adapters.admin.budget import build_budget_router
from teamai.adapters.admin.memory import build_memory_router
from teamai.adapters.admin.policy import build_policy_router
from teamai.adapters.admin.tag import build_tag_router
from teamai.adapters.admin.task import build_task_router
from teamai.container import Container

__all__ = ["build_admin_router"]


def build_admin_router(container: Container) -> APIRouter:
    router = APIRouter(prefix="/api")

    @router.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    for build in (
        build_memory_router,
        build_budget_router,
        build_policy_router,
        build_audit_router,
        build_task_router,
        build_tag_router,
    ):
        router.include_router(build(container))

    return router
