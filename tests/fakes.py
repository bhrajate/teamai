"""纯内存测试替身。

只依赖 domain 层声明的抽象，不 import infrastructure —— 这正是把仓储接口
与 TaskQueue 归位到 domain 后换来的能力：application 层可离线测试。
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import UTC, datetime

from teamai.domain.identity import gen_id
from teamai.domain.models import (
    AuditLog,
    BudgetQuota,
    ChannelInstance,
    McpServer,
    MemoryEntry,
    MemoryType,
    OutboxEntry,
    OutboxOp,
    Task,
    TaskStatus,
)
from teamai.domain.ports import MessagePublisher, QueuePayload, ReplyTarget, TaskQueue
from teamai.domain.repositories import (
    AuditRepository,
    BudgetRepository,
    ChannelRepository,
    McpServerRepository,
    MemoryRepository,
    OutboxRepository,
    OutboxStats,
    TaskRepository,
)


def _utcnow() -> datetime:
    return datetime.now(UTC)


class FakeTaskQueue(TaskQueue):
    """内存队列。

    dequeue 模拟阻塞语义：队列空且带 timeout 时先让出事件循环再返回 None，
    而不是立刻返回 —— 否则 run_worker 会忙转，测试跑满 CPU。真实现是 BLPOP
    挂在 Redis 上等，这里用一次极短 sleep 近似。
    """

    def __init__(self) -> None:
        self.enqueued: list[QueuePayload] = []
        self.fail_next = False
        self.dequeue_timeouts: list[float] = []  # 记录调用方传的超时，供断言

    async def enqueue(self, payload: QueuePayload) -> None:
        if self.fail_next:
            self.fail_next = False
            raise ConnectionError("入队失败（测试注入）")
        self.enqueued.append(payload)

    async def dequeue(self, timeout_seconds: float = 0) -> QueuePayload | None:
        self.dequeue_timeouts.append(timeout_seconds)
        if self.enqueued:
            return self.enqueued.pop(0)
        if timeout_seconds > 0:
            await asyncio.sleep(min(timeout_seconds, 0.01))
        return None


class FakeTaskRepository(TaskRepository):
    def __init__(self) -> None:
        self.items: dict[str, Task] = {}
        self.create_calls = 0
        self.update_calls = 0

    async def create(self, task: Task) -> None:
        self.create_calls += 1
        self.items[task.id] = task

    async def update(self, task: Task) -> None:
        self.update_calls += 1
        self.items[task.id] = task

    async def get(self, task_id: str) -> Task | None:
        return self.items.get(task_id)

    async def list_by_channel(self, channel_instance_id: str, status: TaskStatus | None = None) -> list[Task]:
        out = [t for t in self.items.values() if t.channel_instance_id == channel_instance_id]
        if status is not None:
            out = [t for t in out if t.status is status]
        return out

    async def list_stale(self, statuses: Sequence[TaskStatus], before: datetime) -> list[Task]:
        return [t for t in self.items.values() if t.status in statuses and t.updated_at < before]


class FakeBudgetRepository(BudgetRepository):
    def __init__(self, quota: BudgetQuota | None = None, quotas: list[BudgetQuota] | None = None) -> None:
        self.quota = quota
        self.upserts = 0
        # 周期重置巡检要遍历全部配额；单条场景仍可只传 quota
        self.quotas: list[BudgetQuota] = quotas if quotas is not None else ([quota] if quota else [])

    async def get_for_channel(self, channel_instance_id: str) -> BudgetQuota | None:
        return self.quota

    async def list_all(self) -> list[BudgetQuota]:
        return list(self.quotas)

    async def upsert(self, quota: BudgetQuota) -> None:
        self.upserts += 1
        self.quota = quota
        for i, q in enumerate(self.quotas):
            if q.id == quota.id:
                self.quotas[i] = quota
                return
        self.quotas.append(quota)


class FakeMessagePublisher(MessagePublisher):
    def __init__(self) -> None:
        self.replies: list[tuple[ReplyTarget, str]] = []

    async def reply(self, target: ReplyTarget, text: str) -> None:
        self.replies.append((target, text))


class FakeOutboxRepository(OutboxRepository):
    """内存 outbox 仓储。

    `enqueued` 记下每次入队的 (entry_id, op)，供断言「写入侧确实记下了投影意图」
    —— 这是 outbox 方案的核心保证，替身必须让它可观测。

    租约与退避的真实语义由 tests/unit/test_outbox_repository.py 打真 SQL 锁住，
    这里只做最朴素的实现：`claim` 按入队顺序返回全部未死信记录、不判租约。
    用例若要验租约行为，该去那个文件。
    """

    def __init__(self) -> None:
        self.enqueued: list[tuple[str, OutboxOp]] = []
        self.entries: list[OutboxEntry] = []
        self.completed: list[str] = []
        self.failures: list[tuple[str, str]] = []

    async def enqueue(self, entry_id: str, op: OutboxOp) -> OutboxEntry:
        self.enqueued.append((entry_id, op))
        entry = OutboxEntry(id=gen_id("obx"), entry_id=entry_id, op=op)
        self.entries.append(entry)
        return entry

    async def claim(self, *, limit: int, lease_seconds: int, claimed_by: str) -> list[OutboxEntry]:
        alive = [e for e in self.entries if not e.is_dead]
        taken = alive[:limit]
        for e in taken:
            e.claimed_by = claimed_by
        return taken

    async def complete(self, outbox_id: str) -> None:
        self.completed.append(outbox_id)
        self.entries = [e for e in self.entries if e.id != outbox_id]

    async def fail(
        self, outbox_id: str, error: str, *, max_attempts: int, backoff_seconds: int
    ) -> None:
        self.failures.append((outbox_id, error))
        for e in self.entries:
            if e.id == outbox_id:
                e.attempts += 1
                e.last_error = error
                e.claimed_by = None
                if e.attempts >= max_attempts:
                    e.failed_at = _utcnow()

    async def stats(self) -> OutboxStats:
        alive = [e for e in self.entries if not e.is_dead]
        lag = 0.0
        if alive:
            oldest = min(e.created_at for e in alive)
            lag = max(0.0, (_utcnow() - oldest).total_seconds())
        return OutboxStats(
            pending=len(alive),
            dead=len([e for e in self.entries if e.is_dead]),
            lag_seconds=lag,
        )


class FakeMemoryRepository(MemoryRepository):
    """内存记忆仓储。

    `list_by_channel` 按 created_at 倒序，与 SQL 实现一致 —— 顺序是这个方法的
    契约的一部分（调用方靠它取「最近若干条」），替身若不排序，测试就锁不住它。
    """

    def __init__(self) -> None:
        self.stored: list[MemoryEntry] = []

    async def store(self, entry: MemoryEntry) -> None:
        self.stored.append(entry)

    async def list_by_channel(
        self,
        channel_instance_id: str,
        limit: int | None = None,
        *,
        current_only: bool = True,
        exclude_type: MemoryType | None = None,
    ) -> list[MemoryEntry]:
        out = [e for e in self.stored if e.channel_instance_id == channel_instance_id]
        if current_only:
            out = [e for e in out if e.is_current]
        if exclude_type is not None:
            out = [e for e in out if e.type is not exclude_type]
        out.sort(key=lambda e: e.created_at, reverse=True)
        return out[:limit] if limit is not None else out

    async def get(self, entry_id: str) -> MemoryEntry | None:
        return next((e for e in self.stored if e.id == entry_id), None)

    async def update(self, entry: MemoryEntry) -> None:
        """按 id 原地替换。id 不存在则忽略 —— 与 merge 的语义不同（那个会
        INSERT），但替身不必模拟那个坑，仓储层的 SQL 测试已经覆盖它。"""
        for i, e in enumerate(self.stored):
            if e.id == entry.id:
                self.stored[i] = entry
                return

    async def delete(self, entry_id: str) -> None:
        self.stored = [e for e in self.stored if e.id != entry_id]

    async def find_vector_drift(self, limit: int) -> tuple[list[str], list[str]]:
        """按 should_embed 的语义在内存里判，与 SQL 谓词等价。

        替身这里用 `MemoryEntry.should_embed()` 本身，而 SQL 实现是它的等价改写
        —— 两者是否真的等价由 tests/unit/test_reconciler.py 打真 SQL（注册了
        md5 的 SQLite）穷举 type × superseded 组合来核对，不靠这个替身。

        `content_hash` 从 application 层 import 是刻意的：它就是 SQL 里 `md5()`
        的那份实现，替身若自己算一遍哈希，「两边一致」这个前提就没被测到。
        """
        from teamai.application.projector import content_hash

        rows = sorted(self.stored, key=lambda e: e.created_at)
        missing = [
            e.id
            for e in rows
            if e.should_embed()
            and (e.embedding_ref is None or e.embedded_hash != content_hash(e.content))
        ][:limit]
        stale = [e.id for e in rows if not e.should_embed() and e.embedding_ref is not None][:limit]
        return missing, stale

    async def list_preferences(self, channel_instance_id: str) -> list[MemoryEntry]:
        out = [
            e
            for e in self.stored
            if e.channel_instance_id == channel_instance_id
            and e.type is MemoryType.PREFERENCE
            and e.is_current
        ]
        out.sort(key=lambda e: e.created_at, reverse=True)
        return out


class FakeChannelRepository(ChannelRepository):
    def __init__(self, instances: list[ChannelInstance] | None = None) -> None:
        self.items: dict[str, ChannelInstance] = {i.id: i for i in (instances or [])}

    async def get(self, channel_instance_id: str) -> ChannelInstance | None:
        return self.items.get(channel_instance_id)

    async def list(self) -> list[ChannelInstance]:
        return sorted(self.items.values(), key=lambda i: i.created_at, reverse=True)

    async def get_by_platform_channel(
        self, platform: str, channel_id: str, workspace_id: str
    ) -> ChannelInstance | None:
        return next(
            (
                i
                for i in self.items.values()
                if i.platform == platform
                and i.channel_id == channel_id
                and i.workspace_id == workspace_id
            ),
            None,
        )

    async def upsert(self, instance: ChannelInstance) -> None:
        self.items[instance.id] = instance


class FakeMcpServerRepository(McpServerRepository):
    """内存 MCP server 仓储。upsert 就地更新同 id 行。"""

    def __init__(self, servers: list[McpServer] | None = None) -> None:
        self._servers = {s.id: s for s in servers or []}

    async def list_for_channel(self, channel_instance_id: str) -> list[McpServer]:
        return sorted(
            (s for s in self._servers.values() if s.channel_instance_id == channel_instance_id),
            key=lambda s: s.name,
        )

    async def list_enabled(self) -> list[McpServer]:
        return [s for s in self._servers.values() if s.enabled]

    async def get(self, channel_instance_id: str, server_id: str) -> McpServer | None:
        s = self._servers.get(server_id)
        return s if s and s.channel_instance_id == channel_instance_id else None

    async def find_by_name(self, channel_instance_id: str, name: str) -> McpServer | None:
        return next(
            (s for s in self._servers.values() if s.channel_instance_id == channel_instance_id and s.name == name),
            None,
        )

    async def upsert(self, server: McpServer) -> None:
        self._servers[server.id] = server

    async def delete(self, channel_instance_id: str, server_id: str) -> None:
        self._servers.pop(server_id, None)


class FakeAuditRepository(AuditRepository):
    def __init__(self) -> None:
        self.logs: list[AuditLog] = []

    async def append(self, log: AuditLog) -> None:
        self.logs.append(log)

    async def list_by_channel(self, channel_instance_id: str, limit: int = 100) -> list[AuditLog]:
        out = [x for x in self.logs if x.channel_instance_id == channel_instance_id]
        return out[-limit:]
