"""memory_entries 与 preferences 表（同属记忆聚合）。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Enum, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from teamai.domain.models.memory import MemorySource, MemoryType
from teamai.infrastructure.db import Base


class MemoryEntryModel(Base):
    __tablename__ = "memory_entries"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    channel_instance_id: Mapped[str] = mapped_column(String(32), index=True)
    content: Mapped[str] = mapped_column(Text)
    type: Mapped[MemoryType] = mapped_column(Enum(MemoryType))
    # 飞书 user_id（ou_+32hex）超旧宽度，加宽
    source_user_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # 产生方式。与 source_user_id 是两件事：那个是「哪个用户的话变成了这条」，
    # 这个是「这条是谁写下的」—— 蒸馏产出与管理台写入的 source_user_id 都是
    # NULL，只有这一列能区分它们。
    source: Mapped[MemorySource] = mapped_column(
        Enum(MemorySource), default=MemorySource.MANUAL
    )
    embedding_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # 取代本条的记忆 id。加索引：检索路径一律带 `superseded_by IS NULL`，
    # 这是每次查询都要过的条件。
    #
    # 不做外键：被取代的条目可能因人工删除而消失（MemoryService.delete 是
    # 物理删除），外键会让那次删除失败或级联清掉指针 —— 前者阻断正常运维，
    # 后者丢掉「这条被取代过」这个事实。留一个可能悬空的 id 是有意的取舍，
    # 读取方只用它判 NULL / 非 NULL，解引用失败不影响正确性。
    superseded_by: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    superseded_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class PreferenceModel(Base):
    __tablename__ = "preferences"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    channel_instance_id: Mapped[str] = mapped_column(String(32), index=True)
    # 飞书 user_id（ou_+32hex）超旧宽度，加宽
    user_id: Mapped[str] = mapped_column(String(64))
    preference: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
