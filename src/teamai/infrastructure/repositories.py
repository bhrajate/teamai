"""SQLAlchemy 仓储实现。"""

from __future__ import annotations

import json
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from teamai.domain.audit import AuditLog
from teamai.domain.budget import BudgetQuota
from teamai.domain.channel import ChannelInstance
from teamai.domain.memory import MemoryEntry, Preference
from teamai.domain.policy import AmbientRule, PermissionPolicy
from teamai.domain.tag import TagTemplate
from teamai.domain.task import Task, TaskStatus
from teamai.domain.repositories import (
    AuditRepository,
    BudgetRepository,
    ChannelRepository,
    MemoryRepository,
    PolicyRepository,
    TagRepository,
    TaskRepository,
)
from teamai.infrastructure.orm import (
    AuditLogModel,
    BudgetQuotaModel,
    ChannelInstanceModel,
    MemoryEntryModel,
    PolicyModel,
    PreferenceModel,
    TagTemplateModel,
    TaskModel,
)


def _task_to_model(task: Task) -> TaskModel:
    return TaskModel(
        id=task.id,
        channel_instance_id=task.channel_instance_id,
        thread_ts=task.thread_ts,
        requester_id=task.requester_id,
        intent=task.intent,
        tag_name=task.tag_name,
        model_level=task.model_level,
        status=task.status,
        current_stage=task.current_stage,
        owner_id=task.owner_id,
        canceled_by=task.canceled_by,
        created_at=task.created_at,
        updated_at=task.updated_at,
    )


def _model_to_task(m: TaskModel) -> Task:
    return Task(
        id=m.id,
        channel_instance_id=m.channel_instance_id,
        thread_ts=m.thread_ts,
        requester_id=m.requester_id,
        intent=m.intent,
        tag_name=m.tag_name,
        model_level=m.model_level,
        status=m.status,
        current_stage=m.current_stage,
        owner_id=m.owner_id,
        canceled_by=m.canceled_by,
        created_at=m.created_at,
        updated_at=m.updated_at,
    )


class SQLTaskRepository(TaskRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, task: Task) -> None:
        self._session.add(_task_to_model(task))

    async def update(self, task: Task) -> None:
        await self._session.merge(_task_to_model(task))

    async def get(self, task_id: str) -> Task | None:
        m = await self._session.get(TaskModel, task_id)
        return _model_to_task(m) if m else None

    async def list_by_channel(self, channel_instance_id: str, status: TaskStatus | None = None) -> list[Task]:
        stmt = select(TaskModel).where(TaskModel.channel_instance_id == channel_instance_id)
        if status is not None:
            stmt = stmt.where(TaskModel.status == status)
        rows = (await self._session.execute(stmt)).scalars().all()
        return [_model_to_task(r) for r in rows]


def _memory_to_model(e: MemoryEntry) -> MemoryEntryModel:
    return MemoryEntryModel(
        id=e.id,
        channel_instance_id=e.channel_instance_id,
        content=e.content,
        type=e.type,
        source_user_id=e.source_user_id,
        visibility=e.visibility,
        embedding_ref=e.embedding_ref,
        created_at=e.created_at,
    )


def _model_to_memory(m: MemoryEntryModel) -> MemoryEntry:
    return MemoryEntry(
        id=m.id,
        channel_instance_id=m.channel_instance_id,
        content=m.content,
        type=m.type,
        source_user_id=m.source_user_id,
        visibility=m.visibility,
        embedding_ref=m.embedding_ref,
        created_at=m.created_at,
    )


class SQLMemoryRepository(MemoryRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def store(self, entry: MemoryEntry) -> None:
        self._session.add(_memory_to_model(entry))

    async def list_by_channel(self, channel_instance_id: str) -> list[MemoryEntry]:
        stmt = select(MemoryEntryModel).where(MemoryEntryModel.channel_instance_id == channel_instance_id)
        rows = (await self._session.execute(stmt)).scalars().all()
        return [_model_to_memory(r) for r in rows]

    async def get(self, entry_id: str) -> MemoryEntry | None:
        m = await self._session.get(MemoryEntryModel, entry_id)
        return _model_to_memory(m) if m else None

    async def delete(self, entry_id: str) -> None:
        m = await self._session.get(MemoryEntryModel, entry_id)
        if m:
            await self._session.delete(m)

    async def set_preference(self, pref: Preference) -> None:
        self._session.add(
            PreferenceModel(
                id=pref.id,
                channel_instance_id=pref.channel_instance_id,
                user_id=pref.user_id,
                preference=pref.preference,
                created_at=pref.created_at,
            )
        )

    async def list_preferences(self, channel_instance_id: str) -> list[Preference]:
        stmt = select(PreferenceModel).where(PreferenceModel.channel_instance_id == channel_instance_id)
        rows = (await self._session.execute(stmt)).scalars().all()
        return [
            Preference(
                id=r.id,
                channel_instance_id=r.channel_instance_id,
                user_id=r.user_id,
                preference=r.preference,
                created_at=r.created_at,
            )
            for r in rows
        ]


def _tag_to_model(t: TagTemplate) -> TagTemplateModel:
    return TagTemplateModel(
        id=t.id,
        channel_instance_id=t.channel_instance_id,
        name=t.name,
        instruction=t.instruction,
        role=t.role,
        output_style=t.output_style,
        shared=t.shared,
        created_by=t.created_by,
        active=t.active,
        created_at=t.created_at,
    )


def _model_to_tag(m: TagTemplateModel) -> TagTemplate:
    return TagTemplate(
        id=m.id,
        channel_instance_id=m.channel_instance_id,
        name=m.name,
        instruction=m.instruction,
        role=m.role,
        output_style=m.output_style,
        shared=m.shared,
        created_by=m.created_by,
        active=m.active,
        created_at=m.created_at,
    )


class SQLTagRepository(TagRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, tag: TagTemplate) -> None:
        self._session.add(_tag_to_model(tag))

    async def get(self, channel_instance_id: str, name: str) -> TagTemplate | None:
        stmt = select(TagTemplateModel).where(
            TagTemplateModel.channel_instance_id == channel_instance_id,
            TagTemplateModel.name == name,
        )
        m = (await self._session.execute(stmt)).scalars().first()
        return _model_to_tag(m) if m else None

    async def list_by_channel(self, channel_instance_id: str) -> list[TagTemplate]:
        stmt = select(TagTemplateModel).where(TagTemplateModel.channel_instance_id == channel_instance_id)
        rows = (await self._session.execute(stmt)).scalars().all()
        return [_model_to_tag(r) for r in rows]

    async def delete(self, tag_id: str) -> None:
        m = await self._session.get(TagTemplateModel, tag_id)
        if m:
            await self._session.delete(m)

    async def set_active(self, tag_id: str, active: bool) -> None:
        m = await self._session.get(TagTemplateModel, tag_id)
        if m:
            m.active = active


def _policy_to_model(p: PermissionPolicy) -> PolicyModel:
    return PolicyModel(
        id=p.id,
        channel_instance_id=p.channel_instance_id,
        allowed_tools=json.dumps(p.allowed_tools),
        ambient_rules=json.dumps([r.__dict__ for r in p.ambient_rules]),
        updated_by=p.updated_by,
        updated_at=p.updated_at,
    )


def _model_to_policy(m: PolicyModel) -> PermissionPolicy:
    tools = json.loads(m.allowed_tools or "[]")
    rules = [
        AmbientRule(trigger=r.get("trigger", ""), params=r.get("params", {}), action=r.get("action", "nudge"))
        for r in json.loads(m.ambient_rules or "[]")
    ]
    return PermissionPolicy(
        id=m.id,
        channel_instance_id=m.channel_instance_id,
        allowed_tools=tools,
        ambient_rules=rules,
        updated_by=m.updated_by,
        updated_at=m.updated_at,
    )


class SQLPolicyRepository(PolicyRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_for_channel(self, channel_instance_id: str) -> PermissionPolicy | None:
        stmt = select(PolicyModel).where(PolicyModel.channel_instance_id == channel_instance_id)
        m = (await self._session.execute(stmt)).scalars().first()
        return _model_to_policy(m) if m else None

    async def upsert(self, policy: PermissionPolicy) -> None:
        await self._session.merge(_policy_to_model(policy))


def _budget_to_model(b: BudgetQuota) -> BudgetQuotaModel:
    return BudgetQuotaModel(
        id=b.id,
        scope=b.scope,
        token_limit=b.token_limit,
        period=b.period,
        used_tokens=b.used_tokens,
        channel_instance_id=b.channel_instance_id,
        state=b.state,
        updated_at=b.updated_at,
    )


def _model_to_budget(m: BudgetQuotaModel) -> BudgetQuota:
    return BudgetQuota(
        id=m.id,
        scope=m.scope,
        token_limit=m.token_limit,
        period=m.period,
        used_tokens=m.used_tokens,
        channel_instance_id=m.channel_instance_id,
        state=m.state,
        updated_at=m.updated_at,
    )


class SQLBudgetRepository(BudgetRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_for_channel(self, channel_instance_id: str) -> BudgetQuota | None:
        stmt = select(BudgetQuotaModel).where(
            BudgetQuotaModel.channel_instance_id == channel_instance_id,
            BudgetQuotaModel.scope == "CHANNEL",
        )
        m = (await self._session.execute(stmt)).scalars().first()
        return _model_to_budget(m) if m else None

    async def upsert(self, quota: BudgetQuota) -> None:
        await self._session.merge(_budget_to_model(quota))


class SQLChannelRepository(ChannelRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, channel_instance_id: str) -> ChannelInstance | None:
        m = await self._session.get(ChannelInstanceModel, channel_instance_id)
        return self._to_domain(m) if m else None

    async def get_by_slack(self, channel_id: str, workspace_id: str) -> ChannelInstance | None:
        stmt = select(ChannelInstanceModel).where(
            ChannelInstanceModel.channel_id == channel_id,
            ChannelInstanceModel.workspace_id == workspace_id,
        )
        m = (await self._session.execute(stmt)).scalars().first()
        return self._to_domain(m) if m else None

    async def upsert(self, instance: ChannelInstance) -> None:
        m = ChannelInstanceModel(
            id=instance.id,
            platform=instance.platform,
            channel_id=instance.channel_id,
            workspace_id=instance.workspace_id,
            agent_identity=instance.agent_identity,
            ambient_enabled=instance.ambient_enabled,
            cross_channel_learning=instance.cross_channel_learning,
            policy_id=instance.policy_id,
            created_at=instance.created_at,
        )
        await self._session.merge(m)

    @staticmethod
    def _to_domain(m: ChannelInstanceModel) -> ChannelInstance:
        return ChannelInstance(
            id=m.id,
            platform=m.platform,
            channel_id=m.channel_id,
            workspace_id=m.workspace_id,
            agent_identity=m.agent_identity,
            ambient_enabled=m.ambient_enabled,
            cross_channel_learning=m.cross_channel_learning,
            policy_id=m.policy_id,
            created_at=m.created_at,
        )


def _audit_to_model(a: AuditLog) -> AuditLogModel:
    return AuditLogModel(
        id=a.id,
        channel_instance_id=a.channel_instance_id,
        user_id=a.user_id,
        action=a.action,
        detail=json.dumps(a.detail),
        task_id=a.task_id,
        tokens_consumed=a.tokens_consumed,
        result=a.result,
        ts=a.ts,
    )


def _model_to_audit(m: AuditLogModel) -> AuditLog:
    return AuditLog(
        id=m.id,
        channel_instance_id=m.channel_instance_id,
        user_id=m.user_id,
        action=m.action,
        detail=json.loads(m.detail or "{}"),
        task_id=m.task_id,
        tokens_consumed=m.tokens_consumed,
        result=m.result,
        ts=m.ts,
    )


class SQLAuditRepository(AuditRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def append(self, log: AuditLog) -> None:
        self._session.add(_audit_to_model(log))

    async def list_by_channel(self, channel_instance_id: str, limit: int = 100) -> list[AuditLog]:
        stmt = (
            select(AuditLogModel)
            .where(AuditLogModel.channel_instance_id == channel_instance_id)
            .order_by(AuditLogModel.ts.desc())
            .limit(limit)
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return [_model_to_audit(r) for r in rows]
