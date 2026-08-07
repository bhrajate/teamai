"""tag_templates 表。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from teamai.infrastructure.db import Base


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
