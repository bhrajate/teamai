"""mcp_servers 表。

headers 以 JSON 字符串存储（对齐 permission_policies 的先例），
序列化在 repositories/mcp.py 的 mapper 中完成。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from teamai.infrastructure.db import Base


class McpServerModel(Base):
    __tablename__ = "mcp_servers"
    __table_args__ = (
        # 同频道内 server 名唯一：工具名前缀 ``mcp__<name>__`` 靠它才能无歧义解析
        UniqueConstraint("channel_instance_id", "name", name="uq_mcp_servers_channel_name"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    channel_instance_id: Mapped[str] = mapped_column(String(32), index=True)
    name: Mapped[str] = mapped_column(String(64))
    url: Mapped[str] = mapped_column(String(512))
    headers: Mapped[str] = mapped_column(Text, default="{}")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
