"""permission_policies 表。

allowed_tools 与 ambient_rules 以 JSON 数组字符串存储，
序列化在 repositories/policy.py 的 mapper 中完成。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from teamai.infrastructure.db import Base


class PolicyModel(Base):
    __tablename__ = "permission_policies"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    channel_instance_id: Mapped[str] = mapped_column(String(32), index=True)
    allowed_tools: Mapped[str] = mapped_column(Text, default="[]")
    ambient_rules: Mapped[str] = mapped_column(Text, default="[]")
    # 工具名 → 需要几个批准。JSON 对象字符串（其余两列是 JSON 数组，同一套先例）。
    approval_required_tools: Mapped[str] = mapped_column(Text, default="{}")
    # 频道级审批人 id。JSON 数组字符串。
    approver_ids: Mapped[str] = mapped_column(Text, default="[]")
    updated_by: Mapped[str | None] = mapped_column(String(32), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
