"""Admin API：FastAPI 路由（频道/记忆/预算/策略/审计/标签管理）。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

from teamai.container import Container
from teamai.domain.budget import BudgetPeriod, BudgetQuota, BudgetScope
from teamai.domain.policy import AmbientRule, PermissionPolicy
from teamai.util.events import gen_id


def build_admin_router(container: Container) -> APIRouter:
    router = APIRouter(prefix="/api")

    @router.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    # ---- 记忆管理 ----

    @router.get("/channels/{channel_instance_id}/memories")
    async def list_memories(channel_instance_id: str) -> list[dict[str, Any]]:
        entries = await container.memory.list(channel_instance_id)
        return [_memory_to_dict(e) for e in entries]

    @router.post("/channels/{channel_instance_id}/memories")
    async def create_memory(channel_instance_id: str, body: dict[str, Any]) -> dict[str, Any]:
        content = body.get("content", "")
        if not content:
            raise HTTPException(status_code=400, detail="content 不能为空")
        entry = await container.memory.store(channel_instance_id, content, source_user_id=body.get("user_id"))
        return _memory_to_dict(entry)

    @router.delete("/memories/{entry_id}")
    async def delete_memory(entry_id: str, actor: str | None = None) -> dict[str, str]:
        await container.memory.delete(entry_id, actor=actor)
        return {"status": "deleted"}

    # ---- 预算管理 ----

    @router.get("/channels/{channel_instance_id}/budget")
    async def get_budget(channel_instance_id: str) -> dict[str, Any]:
        quota = await container.budget.get_quota(channel_instance_id)
        if quota is None:
            raise HTTPException(status_code=404, detail="该频道未配置预算")
        return _budget_to_dict(quota)

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
        return _budget_to_dict(quota)

    # ---- 权限策略 ----

    @router.get("/channels/{channel_instance_id}/policy")
    async def get_policy(channel_instance_id: str) -> dict[str, Any]:
        policy = await container.policy_repo.get_for_channel(channel_instance_id)
        if policy is None:
            raise HTTPException(status_code=404, detail="该频道未配置策略")
        return _policy_to_dict(policy)

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
        return _policy_to_dict(policy)

    # ---- 审计 ----

    @router.get("/channels/{channel_instance_id}/audit")
    async def list_audit(channel_instance_id: str, limit: int = 100) -> list[dict[str, Any]]:
        logs = await container.audit_repo.list_by_channel(channel_instance_id, limit=limit)
        return [_audit_to_dict(log) for log in logs]

    # ---- 任务 ----

    @router.get("/channels/{channel_instance_id}/tasks")
    async def list_tasks(channel_instance_id: str) -> list[dict[str, Any]]:
        tasks = await container.orchestrator.list(channel_instance_id)
        return [_task_to_dict(t) for t in tasks]

    # ---- 标签模板 ----

    @router.get("/channels/{channel_instance_id}/tags")
    async def list_tags(channel_instance_id: str) -> list[dict[str, Any]]:
        tags = await container.tags.list(channel_instance_id)
        return [_tag_to_dict(t) for t in tags]

    @router.post("/channels/{channel_instance_id}/tags")
    async def create_tag(channel_instance_id: str, body: dict[str, Any]) -> dict[str, Any]:
        tag = await container.tags.create(
            channel_instance_id,
            body.get("name", ""),
            body.get("instruction", ""),
            role=body.get("role"),
            output_style=body.get("output_style"),
            created_by=body.get("created_by"),
        )
        return _tag_to_dict(tag)

    return router


def _memory_to_dict(entry) -> dict[str, Any]:
    return {
        "id": entry.id,
        "channel_instance_id": entry.channel_instance_id,
        "content": entry.content,
        "type": entry.type.value,
        "source_user_id": entry.source_user_id,
        "created_at": entry.created_at.isoformat(),
    }


def _budget_to_dict(q: BudgetQuota) -> dict[str, Any]:
    return {
        "id": q.id,
        "scope": q.scope.value,
        "token_limit": q.token_limit,
        "period": q.period.value,
        "used_tokens": q.used_tokens,
        "remaining": q.remaining,
        "state": q.state.value,
    }


def _policy_to_dict(p: PermissionPolicy) -> dict[str, Any]:
    return {
        "id": p.id,
        "channel_instance_id": p.channel_instance_id,
        "allowed_tools": p.allowed_tools,
        "ambient_rules": [{"trigger": r.trigger, "params": r.params, "action": r.action} for r in p.ambient_rules],
        "updated_at": p.updated_at.isoformat(),
    }


def _audit_to_dict(log) -> dict[str, Any]:
    return {
        "id": log.id,
        "ts": log.ts.isoformat(),
        "channel_instance_id": log.channel_instance_id,
        "user_id": log.user_id,
        "action": log.action.value,
        "task_id": log.task_id,
        "tokens_consumed": log.tokens_consumed,
        "result": log.result.value,
        "detail": log.detail,
    }


def _task_to_dict(t) -> dict[str, Any]:
    return {
        "id": t.id,
        "channel_instance_id": t.channel_instance_id,
        "intent": t.intent,
        "status": t.status.value,
        "tag_name": t.tag_name,
        "model_level": t.model_level,
        "requester_id": t.requester_id,
        "created_at": t.created_at.isoformat(),
        "updated_at": t.updated_at.isoformat(),
    }


def _tag_to_dict(t) -> dict[str, Any]:
    return {
        "id": t.id,
        "channel_instance_id": t.channel_instance_id,
        "name": t.name,
        "instruction": t.instruction,
        "role": t.role,
        "output_style": t.output_style,
        "shared": t.shared,
        "active": t.active,
        "created_by": t.created_by,
    }
