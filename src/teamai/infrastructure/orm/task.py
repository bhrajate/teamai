"""tasks 表。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Enum, String
from sqlalchemy.orm import Mapped, mapped_column

from teamai.domain.models.task import TaskStatus
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
