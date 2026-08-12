"""纯内存测试替身。

只依赖 domain 层声明的抽象，不 import infrastructure —— 这正是把仓储接口
与 TaskQueue 归位到 domain 后换来的能力：application 层可离线测试。
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import datetime

from teamai.domain.models import (
    AuditLog,
    BudgetQuota,
    ChannelInstance,
    MemoryEntry,
    Preference,
    Task,
    TaskStatus,
)
from teamai.domain.ports import MessagePublisher, QueuePayload, ReplyTarget, TaskQueue
from teamai.domain.repositories import (
    AuditRepository,
    BudgetRepository,
    ChannelRepository,
    MemoryRepository,
    TaskRepository,
)


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


class FakeMemoryRepository(MemoryRepository):
    """内存记忆仓储。

    `list_by_channel` 按 created_at 倒序，与 SQL 实现一致 —— 顺序是这个方法的
    契约的一部分（调用方靠它取「最近若干条」），替身若不排序，测试就锁不住它。
    """

    def __init__(self) -> None:
        self.stored: list[MemoryEntry] = []
        self.prefs: list[Preference] = []

    async def store(self, entry: MemoryEntry) -> None:
        self.stored.append(entry)

    async def list_by_channel(
        self, channel_instance_id: str, limit: int | None = None
    ) -> list[MemoryEntry]:
        out = [e for e in self.stored if e.channel_instance_id == channel_instance_id]
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

    async def set_preference(self, pref: Preference) -> None:
        self.prefs.append(pref)

    async def list_preferences(self, channel_instance_id: str) -> list[Preference]:
        return [p for p in self.prefs if p.channel_instance_id == channel_instance_id]


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


class FakeAuditRepository(AuditRepository):
    def __init__(self) -> None:
        self.logs: list[AuditLog] = []

    async def append(self, log: AuditLog) -> None:
        self.logs.append(log)

    async def list_by_channel(self, channel_instance_id: str, limit: int = 100) -> list[AuditLog]:
        out = [x for x in self.logs if x.channel_instance_id == channel_instance_id]
        return out[-limit:]
