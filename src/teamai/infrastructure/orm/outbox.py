"""memory_outbox 表:记忆向量投影的待办队列。

与 memory_entries 同事务写入，由 worker 里的常驻 projector 消费。
设计见 docs/plan-memory-outbox.md §5.3。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Enum, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from teamai.domain.models.outbox import OutboxOp
from teamai.infrastructure.db import Base


class MemoryOutboxModel(Base):
    __tablename__ = "memory_outbox"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    # 不做外键:记忆被物理删除后这条仍要被处理 —— projector 回读为空即触发
    # 删向量，这是 delete 路径的正确语义。加外键会让删除失败或级联清掉这条，
    # 前者阻断运维，后者留下孤儿向量。与 memory_entries.superseded_by 同类取舍。
    entry_id: Mapped[str] = mapped_column(String(32), index=True)
    # ⚠️ 仅可观测，projector 不据此行事。理由见 domain/models/outbox.py 的
    # OutboxOp 注释。
    op: Mapped[OutboxOp] = mapped_column(Enum(OutboxOp))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    # 退避到点才可取。加索引:抢占查询每次都按它过滤。
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    claimed_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 非空即死信。加索引:抢占查询与 pending 统计都要排除死信。
    failed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    # lag 指标由它算（now() - min(created_at)）。加索引:抢占按它排序。
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
