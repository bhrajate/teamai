"""领域对象 → JSON 可序列化 dict。

Admin API 对外的字段形状集中在这里，路由模块只负责取数据和调用本模块，
避免同一个领域对象在多个路由里被拼成不同形状。
"""

from __future__ import annotations

from typing import Any

from teamai.domain.models import (
    AgentInteraction,
    AuditLog,
    BudgetQuota,
    ChannelInstance,
    McpServer,
    MemoryEntry,
    PermissionPolicy,
    TagTemplate,
    Task,
)

# headers 中表示「保留原值」的占位值（见 mcp.py 路由的更新语义）
HEADER_PLACEHOLDER = "***"


def mcp_server_to_dict(server: McpServer) -> dict[str, Any]:
    """headers 一律脱敏回显 —— 凭据经前端往返一次就暴露一份，不该出现在任何
    响应里；更新时填 ``***`` 表示保留原值（见 build_mcp_router.set_server）。"""
    return {
        "id": server.id,
        "channel_instance_id": server.channel_instance_id,
        "name": server.name,
        "url": server.url,
        "headers": {k: HEADER_PLACEHOLDER for k in server.headers},
        "enabled": server.enabled,
        "last_error": server.last_error,
        "created_at": server.created_at.isoformat(),
        "updated_at": server.updated_at.isoformat(),
    }


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
        # 产生方式（DISTILLED / MANUAL / EDITED）。与 source_user_id 并列输出：
        # 后者对蒸馏产出与管理台写入都是 null，只有这个字段能区分它们。
        "source": entry.source.value,
        # 有值即已建向量索引，据此能查出漏索引的条目
        "embedding_ref": entry.embedding_ref,
        # 取代本条的记忆 id。非 null 即表示本条已不是现行事实、不再参与检索，
        # 但仍留在库里供排查「这条事实之前是什么」。
        "superseded_by": entry.superseded_by,
        "superseded_at": entry.superseded_at.isoformat() if entry.superseded_at else None,
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


def interaction_to_dict(record: AgentInteraction) -> dict[str, Any]:
    """交互记录 → dict。

    提示词与响应全文原样带出：这个端点的用途就是复现「模型当时看到了什么」，
    截断会让它失去意义。前端负责折叠显示。

    token 同时给分项与合计：分项用于按单价核算成本（输入输出差数倍），
    合计用于与 audit_logs.tokens_consumed 对照。
    """
    return {
        "id": record.id,
        "task_id": record.task_id,
        "channel_instance_id": record.channel_instance_id,
        "thread_ref": record.thread_ref,
        "requester_id": record.requester_id,
        "user_prompt": record.user_prompt,
        "system_prompt": record.system_prompt,
        "response": record.response,
        "model_level": record.model_level,
        "model_id": record.model_id,
        "tokens_in": record.tokens_in,
        "tokens_out": record.tokens_out,
        "tokens_total": record.tokens_total,
        "result": record.result.value,
        "error": record.error,
        "context_refs": record.context_refs,
        "created_at": record.created_at.isoformat(),
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
