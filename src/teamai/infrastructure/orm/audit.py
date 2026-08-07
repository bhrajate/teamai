"""audit_logs 表。

detail 以 JSON 字符串存储，序列化在 repositories/audit.py 的 mapper 中完成。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Enum, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from teamai.domain.models.audit import AuditAction, AuditResult
from teamai.infrastructure.db import Base


class AuditLogModel(Base):
    __tablename__ = "audit_logs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    channel_instance_id: Mapped[str] = mapped_column(String(32), index=True)
    user_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    action: Mapped[AuditAction] = mapped_column(Enum(AuditAction))
    detail: Mapped[str] = mapped_column(Text, default="{}")
    task_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    tokens_consumed: Mapped[int] = mapped_column(Integer, default=0)
    result: Mapped[AuditResult] = mapped_column(Enum(AuditResult), default=AuditResult.SUCCESS)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
