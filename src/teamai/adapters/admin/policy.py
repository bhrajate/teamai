"""权限策略路由。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

from teamai.adapters.admin.serializers import policy_to_dict
from teamai.container import Container
from teamai.domain.identity import gen_id
from teamai.domain.models import AmbientRule, PermissionPolicy


def build_policy_router(container: Container) -> APIRouter:
    router = APIRouter()

    @router.get("/channels/{channel_instance_id}/policy")
    async def get_policy(channel_instance_id: str) -> dict[str, Any]:
        policy = await container.policy_repo.get_for_channel(channel_instance_id)
        if policy is None:
            raise HTTPException(status_code=404, detail="该频道未配置策略")
        return policy_to_dict(policy)

    @router.put("/channels/{channel_instance_id}/policy")
    async def set_policy(channel_instance_id: str, body: dict[str, Any]) -> dict[str, Any]:
        rules = [
            AmbientRule(trigger=r.get("trigger", ""), params=r.get("params", {}), action=r.get("action", "nudge"))
            for r in body.get("ambient_rules", [])
        ]
        policy = PermissionPolicy(
            id=gen_id("pol"),
            channel_instance_id=channel_instance_id,
            allowed_tools=list(body.get("allowed_tools", [])),
            ambient_rules=rules,
            updated_by=body.get("actor"),
        )
        await container.policy_repo.upsert(policy)
        return policy_to_dict(policy)

    return router
