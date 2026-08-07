"""ORM 表模型（对应领域模型映射）。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Enum, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from teamai.domain.audit import AuditAction, AuditResult
from teamai.domain.budget import BudgetPeriod, BudgetScope, BudgetState
from teamai.domain.memory import MemoryType, Visibility
from teamai.domain.task import TaskStatus
from teamai.infrastructure.db import Base


class TaskModel(Base):
    __tablename__ = "tasks"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    channel_instance_id: Mapped[str] = mapped_column(String(32), index=True)
    thread_ts: Mapped[str] = mapped_column(String(32))
    requester_id: Mapped[str] = mapped_column(String(32))
    intent: Mapped[str] = mapped_column(String(64))
    tag_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    model_level: Mapped[str] = mapped_column(String(16), default="light")
    status: Mapped[TaskStatus] = mapped_column(Enum(TaskStatus), default=TaskStatus.PENDING)
    current_stage: Mapped[str | None] = mapped_column(String(64), nullable=True)
    owner_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    canceled_by: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ChannelInstanceModel(Base):
    __tablename__ = "channel_instances"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    platform: Mapped[str] = mapped_column(String(16))
    channel_id: Mapped[str] = mapped_column(String(32), index=True)
    workspace_id: Mapped[str] = mapped_column(String(32), index=True)
    agent_identity: Mapped[str] = mapped_column(String(32))
    ambient_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    cross_channel_learning: Mapped[bool] = mapped_column(Boolean, default=False)
    policy_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class MemoryEntryModel(Base):
    __tablename__ = "memory_entries"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    channel_instance_id: Mapped[str] = mapped_column(String(32), index=True)
    content: Mapped[str] = mapped_column(Text)
    type: Mapped[MemoryType] = mapped_column(Enum(MemoryType))
    source_user_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    visibility: Mapped[Visibility] = mapped_column(Enum(Visibility), default=Visibility.CHANNEL)
    embedding_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class PreferenceModel(Base):
    __tablename__ = "preferences"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    channel_instance_id: Mapped[str] = mapped_column(String(32), index=True)
    user_id: Mapped[str] = mapped_column(String(32))
    preference: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class TagTemplateModel(Base):
    __tablename__ = "tag_templates"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    channel_instance_id: Mapped[str] = mapped_column(String(32), index=True)
    name: Mapped[str] = mapped_column(String(64))
    instruction: Mapped[str] = mapped_column(Text)
    role: Mapped[str | None] = mapped_column(String(64), nullable=True)
    output_style: Mapped[str | None] = mapped_column(String(64), nullable=True)
    shared: Mapped[bool] = mapped_column(Boolean, default=True)
    created_by: Mapped[str | None] = mapped_column(String(32), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class PolicyModel(Base):
    __tablename__ = "permission_policies"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    channel_instance_id: Mapped[str] = mapped_column(String(32), index=True)
    allowed_tools: Mapped[str] = mapped_column(Text, default="[]")  # JSON 数组字符串
    ambient_rules: Mapped[str] = mapped_column(Text, default="[]")  # JSON 数组字符串
    updated_by: Mapped[str | None] = mapped_column(String(32), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class BudgetQuotaModel(Base):
    __tablename__ = "budget_quotas"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    scope: Mapped[BudgetScope] = mapped_column(Enum(BudgetScope))
    token_limit: Mapped[int] = mapped_column(Integer)
    period: Mapped[BudgetPeriod] = mapped_column(Enum(BudgetPeriod))
    used_tokens: Mapped[int] = mapped_column(Integer, default=0)
    channel_instance_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    state: Mapped[BudgetState] = mapped_column(Enum(BudgetState), default=BudgetState.ACTIVE)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class AuditLogModel(Base):
    __tablename__ = "audit_logs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    channel_instance_id: Mapped[str] = mapped_column(String(32), index=True)
    user_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    action: Mapped[AuditAction] = mapped_column(Enum(AuditAction))
    detail: Mapped[str] = mapped_column(Text, default="{}")  # JSON 字符串
    task_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    tokens_consumed: Mapped[int] = mapped_column(Integer, default=0)
    result: Mapped[AuditResult] = mapped_column(Enum(AuditResult), default=AuditResult.SUCCESS)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
