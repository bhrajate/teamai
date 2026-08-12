"""agent_interactions 表。

与 audit_logs 分表的理由见 domain/models/interaction.py：一张记动作流水
（永久、字段窄），一张记内容快照（含全文、按保留期清理）。

context_refs 以 JSON 字符串存储，序列化在 repositories/interaction.py 的
mapper 中完成 —— 与 audit_logs 的 detail 同一做法，避免绑定 Postgres 的
JSONB 类型（仓储测试跑在 SQLite 上）。

暂不做分区：默认保留期 90 天下单表规模有限。等真实数据量证明需要再按
created_at 做 RANGE 分区。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Enum, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from teamai.domain.models.interaction import InteractionResult
from teamai.infrastructure.db import Base


class AgentInteractionModel(Base):
    __tablename__ = "agent_interactions"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    task_id: Mapped[str] = mapped_column(String(32), index=True)
    channel_instance_id: Mapped[str] = mapped_column(String(32), index=True)
    # thread_ref 取值由各平台决定：slack 是 thread_ts（形如 1700000000.000100），
    # feishu 是 message_id（om_ + 32hex）。给到 128 留足余量。
    thread_ref: Mapped[str] = mapped_column(String(128))
    # 飞书 user_id（ou_+32hex）需要加宽，与其他表一致
    requester_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_prompt: Mapped[str] = mapped_column(Text)
    system_prompt: Mapped[str] = mapped_column(Text)
    context_refs: Mapped[str] = mapped_column(Text, default="{}")
    model_level: Mapped[str] = mapped_column(String(16))
    # 实际生效的模型 ID（含 provider 前缀），可能是 light 档降级后的备用模型
    model_id: Mapped[str] = mapped_column(String(128), default="")
    response: Mapped[str] = mapped_column(Text, default="")
    tokens_in: Mapped[int] = mapped_column(Integer, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, default=0)
    result: Mapped[InteractionResult] = mapped_column(
        Enum(InteractionResult), default=InteractionResult.DONE
    )
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)

    __table_args__ = (
        # 控制台按频道倒序翻页、保留期清理按时间扫，两者都吃这个复合索引。
        # 单列 index 已经建在 channel_instance_id 与 created_at 上，但复合索引
        # 才能让「某频道最近 50 条」不必先取该频道全部行再排序。
        Index("ix_agent_interactions_channel_created", "channel_instance_id", "created_at"),
    )
