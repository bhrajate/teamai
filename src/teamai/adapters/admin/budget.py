"""预算管理路由。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

from teamai.adapters.admin.serializers import budget_to_dict
from teamai.container import Container
from teamai.domain.models import BudgetPeriod


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
        """设定配额。已配过则原地改上限与周期，用量不清零（见 configure_channel_quota）。"""
        try:
            token_limit = int(body.get("token_limit", 0))
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="token_limit 必须为正整数") from None
        if token_limit <= 0:
            raise HTTPException(status_code=400, detail="token_limit 必须为正整数")

        raw_period = body.get("period", "MONTHLY")
        try:
            period = BudgetPeriod(raw_period)
        except ValueError:
            allowed = "、".join(p.value for p in BudgetPeriod)
            raise HTTPException(
                status_code=400, detail=f"period 取值须为 {allowed}，收到 {raw_period!r}"
            ) from None

        quota = await container.budget.configure_channel_quota(
            channel_instance_id,
            token_limit,
            period,
            actor=body.get("actor"),
        )
        return budget_to_dict(quota)

    return router
