"""预算管理路由。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

from teamai.adapters.admin.serializers import budget_to_dict
from teamai.container import Container
from teamai.domain.identity import gen_id
from teamai.domain.models import BudgetPeriod, BudgetQuota, BudgetScope


def build_budget_router(container: Container) -> APIRouter:
    router = APIRouter()

    @router.get("/channels/{channel_instance_id}/budget")
    async def get_budget(channel_instance_id: str) -> dict[str, Any]:
        quota = await container.budget.get_quota(channel_instance_id)
        if quota is None:
            raise HTTPException(status_code=404, detail="该频道未配置预算")
        return budget_to_dict(quota)

    @router.put("/channels/{channel_instance_id}/budget")
    async def set_budget(channel_instance_id: str, body: dict[str, Any]) -> dict[str, Any]:
        token_limit = int(body.get("token_limit", 0))
        if token_limit <= 0:
            raise HTTPException(status_code=400, detail="token_limit 必须为正整数")
        quota = BudgetQuota(
            id=gen_id("bq"),
            scope=BudgetScope.CHANNEL,
            token_limit=token_limit,
            period=BudgetPeriod(body.get("period", "MONTHLY")),
            channel_instance_id=channel_instance_id,
        )
        await container.budget.set_quota(quota)
        return budget_to_dict(quota)

    return router
