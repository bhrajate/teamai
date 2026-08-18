"""记忆投影 outbox 的领域模型。

记忆写入与「该重算向量」这个意图落进同一事务，由常驻 projector 异步消费。
设计与取舍见 `docs/plan-memory-outbox.md` §5。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum


def _utcnow() -> datetime:
    return datetime.now(UTC)


class OutboxOp(Enum):
    """入队时的操作类型。

    ⚠️ **仅作可观测信息，永不作为指令。** projector 的动作完全由
    `memory_entries` 的当前状态决定（见 plan-memory-outbox.md §5.2）——
    一旦按本字段行事，滞后的 UPSERT 就会拿旧内容覆盖新向量，而那正是
    本方案要消除的 bug 类。

    保留它是因为排查时「这条是什么操作入的队」有用：同一个 entry_id 连续
    出现 UPSERT 与 DELETE，能一眼看出中间发生过取代或删除。
    """

    UPSERT = "UPSERT"
    DELETE = "DELETE"


@dataclass
class OutboxEntry:
    id: str
    entry_id: str
    op: OutboxOp
    attempts: int = 0
    # 退避到点才可被取。新记录即 now()，即刻可取。
    next_attempt_at: datetime = field(default_factory=_utcnow)
    # 租约起点。非空且未过期表示已被某个 projector 领走。
    # 用租约而非 `FOR UPDATE SKIP LOCKED` 长事务：embed 是远程调用，
    # 持锁等它意味着占着一个数据库连接几十秒（见 §5.4）。
    claimed_at: datetime | None = None
    claimed_by: str | None = None
    last_error: str | None = None
    # 非空即死信，不再被取。不另建死信表：多一张表只是多一处要同步的形状。
    failed_at: datetime | None = None
    created_at: datetime = field(default_factory=_utcnow)

    @property
    def is_dead(self) -> bool:
        return self.failed_at is not None
