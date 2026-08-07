"""channel_instances 表。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from teamai.infrastructure.db import Base


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
