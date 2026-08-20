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

    @router.get("/tools")
    async def list_tools() -> list[str]:
        """已注册的全部工具名，供策略编辑器出选项。

        没有它前端只能硬编码一份工具名清单，`build_tools()` 增删工具时会静默漂移。
        """
        return container.tools.names

    @router.get("/channels/{channel_instance_id}/policy")
    async def get_policy(channel_instance_id: str) -> dict[str, Any]:
        policy = await container.policy_repo.get_for_channel(channel_instance_id)
        if policy is None:
            raise HTTPException(status_code=404, detail="该频道未配置策略")
        return policy_to_dict(policy)

    def _parse_approvals(raw: object) -> dict[str, int]:
        """解析「工具名 → 需要几个批准」。

        值必须是正整数：0 等于不需要审批（那就别配这个工具），负数无意义。
        前端可能传字符串（表单值），故过一层 int()。

        **不校验工具名是否已注册**：MCP 工具在 worker 启动后才存在，而策略可以
        先配。这与 allowed_tools 的现有语义一致 —— 未注册的名字被 registry
        自然忽略。
        """
        if raw is None:
            return {}
        if not isinstance(raw, dict):
            raise HTTPException(
                status_code=422, detail="approval_required_tools 必须是「工具名 → 批准数」的对象"
            )
        out: dict[str, int] = {}
        for name, value in raw.items():
            try:
                count = int(value)
            except (TypeError, ValueError):
                raise HTTPException(
                    status_code=422, detail=f"{name} 的批准数不是整数：{value!r}"
                ) from None
            if count < 1:
                raise HTTPException(
                    status_code=422,
                    detail=f"{name} 的批准数须 >= 1（不需要审批就不要配这个工具）",
                )
            out[str(name)] = count
        return out

    @router.put("/channels/{channel_instance_id}/policy")
    async def set_policy(channel_instance_id: str, body: dict[str, Any]) -> dict[str, Any]:
        rules = [
            AmbientRule(trigger=r.get("trigger", ""), params=r.get("params", {}), action=r.get("action", "nudge"))
            for r in body.get("ambient_rules", [])
        ]
        approvals = _parse_approvals(body.get("approval_required_tools"))
        approvers = [str(x) for x in body.get("approver_ids", []) if str(x).strip()]
        if approvals and not approvers:
            # 配了需审批的工具却没有审批人 = 那些工具永远不能执行。运行时会正确
            # 地拒绝（不放宽），但那是运行时才发现；在这里挡住更省事。
            raise HTTPException(
                status_code=422,
                detail=(
                    "配置了需审批的工具但没有配审批人 —— 那些工具将永远无法执行。"
                    "请同时配置 approver_ids。"
                ),
            )

        policy = PermissionPolicy(
            id=gen_id("pol"),
            channel_instance_id=channel_instance_id,
            allowed_tools=list(body.get("allowed_tools", [])),
            ambient_rules=rules,
            approval_required_tools=approvals,
            approver_ids=approvers,
            updated_by=body.get("actor"),
        )
        await container.policy_repo.upsert(policy)
        return policy_to_dict(policy)

    return router
