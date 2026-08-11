"""领域对象 → JSON 可序列化 dict。

Admin API 对外的字段形状集中在这里，路由模块只负责取数据和调用本模块，
避免同一个领域对象在多个路由里被拼成不同形状。
"""

from __future__ import annotations

from typing import Any

from teamai.domain.models import (
    AuditLog,
    BudgetQuota,
    ChannelInstance,
    MemoryEntry,
    PermissionPolicy,
    TagTemplate,
    Task,
)


def channel_to_dict(instance: ChannelInstance) -> dict[str, Any]:
    return {
        "id": instance.id,
        "platform": instance.platform,
        "channel_id": instance.channel_id,
        "workspace_id": instance.workspace_id,
        "agent_identity": instance.agent_identity,
        "ambient_enabled": instance.ambient_enabled,
        "cross_channel_learning": instance.cross_channel_learning,
        "policy_id": instance.policy_id,
        "created_at": instance.created_at.isoformat(),
    }


def memory_to_dict(entry: MemoryEntry) -> dict[str, Any]:
    return {
        "id": entry.id,
        "channel_instance_id": entry.channel_instance_id,
        "content": entry.content,
        "type": entry.type.value,
        "source_user_id": entry.source_user_id,
        "created_at": entry.created_at.isoformat(),
    }


def budget_to_dict(quota: BudgetQuota) -> dict[str, Any]:
    return {
        "id": quota.id,
        "scope": quota.scope.value,
        "token_limit": quota.token_limit,
        "period": quota.period.value,
        "used_tokens": quota.used_tokens,
        "remaining": quota.remaining,
        "state": quota.state.value,
    }


def policy_to_dict(policy: PermissionPolicy) -> dict[str, Any]:
    return {
        "id": policy.id,
        "channel_instance_id": policy.channel_instance_id,
        "allowed_tools": policy.allowed_tools,
        "ambient_rules": [
            {"trigger": r.trigger, "params": r.params, "action": r.action} for r in policy.ambient_rules
        ],
        "updated_at": policy.updated_at.isoformat(),
    }


def audit_to_dict(log: AuditLog) -> dict[str, Any]:
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


def task_to_dict(task: Task) -> dict[str, Any]:
    return {
        "id": task.id,
        "channel_instance_id": task.channel_instance_id,
        "intent": task.intent,
        "status": task.status.value,
        "tag_name": task.tag_name,
        "model_level": task.model_level,
        "requester_id": task.requester_id,
        "created_at": task.created_at.isoformat(),
        "updated_at": task.updated_at.isoformat(),
    }


def tag_to_dict(tag: TagTemplate) -> dict[str, Any]:
    return {
        "id": tag.id,
        "channel_instance_id": tag.channel_instance_id,
        "name": tag.name,
        "instruction": tag.instruction,
        "role": tag.role,
        "output_style": tag.output_style,
        "shared": tag.shared,
        "active": tag.active,
        "created_by": tag.created_by,
    }
