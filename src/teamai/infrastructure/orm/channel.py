"""channel_instances 表。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from teamai.infrastructure.db import Base


class ChannelInstanceModel(Base):
    __tablename__ = "channel_instances"

    # 复合唯一约束：同一平台同一频道只应有一个实例。get_or_create 并发下若只靠
    # 应用层先查后插，两条请求会同时通过检查并各自插入，撞出重复实例。
    __table_args__ = (
        UniqueConstraint("platform", "workspace_id", "channel_id", name="uq_channel_platform_ws_ch"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    platform: Mapped[str] = mapped_column(String(16))
    # 飞书 ID 为前缀 + 32hex（oc_/ou_/cli_ 等共 33-35 字符），装不下旧 String(32)
    channel_id: Mapped[str] = mapped_column(String(64))
    workspace_id: Mapped[str] = mapped_column(String(64))
    agent_identity: Mapped[str] = mapped_column(String(32))
    ambient_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    cross_channel_learning: Mapped[bool] = mapped_column(Boolean, default=False)
    policy_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
