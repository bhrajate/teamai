"""memory_entries 与 preferences 表（同属记忆聚合）。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Enum, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from teamai.domain.models.memory import MemoryType, Visibility
from teamai.infrastructure.db import Base


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
