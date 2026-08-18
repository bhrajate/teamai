"""记忆投影 outbox 的仓储抽象。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from teamai.domain.models.outbox import OutboxEntry, OutboxOp


@dataclass(frozen=True)
class OutboxStats:
    """供 lag 指标用的聚合。

    `lag_seconds` 取**最老待处理条目**的等待时长，不取平均:平均值会被大量刚
    入队的记录稀释，而要答的问题是「最坏情况下一条记忆多久能被搜到」。
    队列为空时为 0.0。
    """

    pending: int
    dead: int
    lag_seconds: float


class OutboxRepository(ABC):
    @abstractmethod
    async def enqueue(self, entry_id: str, op: OutboxOp) -> OutboxEntry:
        """入队一条投影意图。

        ⚠️ 必须与记忆写入落在**同一事务**里 —— 这是整个方案的核心保证。
        实现方只 flush 不 commit，边界由 `UnitOfWork` 在用例层控制。

        同一 entry_id 允许有多条记录（连续 edit 会产生多条），不去重:去重要在
        写入侧查 outbox，把「写记忆」变成读写混合，而重复处理的代价只是几次多余
        的 embed 调用（后处理的那次按当前内容重算，结果相同）。
        """
        ...

    @abstractmethod
    async def claim(self, *, limit: int, lease_seconds: int, claimed_by: str) -> list[OutboxEntry]:
        """领取一批待处理记录，打上租约。

        取的条件:非死信、`next_attempt_at` 已到、且未被他人持有有效租约。
        按 `created_at` 升序 —— 最老的先处理，与 lag 指标的定义一致。

        用租约而非在整个处理期间持有行锁:embed 是远程调用，持锁等它意味着占着
        一个数据库连接几十秒，而连接池有限（见 docs/plan-memory-outbox.md §5.4）。
        租约过期即自动可被重新领取，projector 崩溃后不需要额外的清理任务。
        """
        ...

    @abstractmethod
    async def complete(self, outbox_id: str) -> None:
        """处理成功。**删行**，不留已完成记录。

        审计由 audit_logs 承担，outbox 只是待办队列。留着已完成记录会让 lag
        查询要额外带状态过滤，且这张表会无界增长。
        """
        ...

    @abstractmethod
    async def fail(
        self, outbox_id: str, error: str, *, max_attempts: int, backoff_seconds: int
    ) -> None:
        """处理失败:递增 attempts、按退避推后 next_attempt_at、释放租约。

        `attempts` 达到 `max_attempts` 时置 `failed_at` 转入死信，此后不再被领取。
        """
        ...

    @abstractmethod
    async def stats(self) -> OutboxStats:
        """供指标用的聚合。只读。"""
        ...
