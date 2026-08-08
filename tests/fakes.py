"""纯内存测试替身。

只依赖 domain 层声明的抽象，不 import infrastructure —— 这正是把仓储接口
与 TaskQueue 归位到 domain 后换来的能力：application 层可离线测试。
"""

from __future__ import annotations

from teamai.domain.models import AuditLog, BudgetQuota, Task, TaskStatus
from teamai.domain.ports import MessagePublisher, QueuePayload, ReplyTarget, TaskQueue
from teamai.domain.repositories import AuditRepository, BudgetRepository, TaskRepository


class FakeTaskQueue(TaskQueue):
    def __init__(self) -> None:
        self.enqueued: list[QueuePayload] = []
        self.fail_next = False

    async def enqueue(self, payload: QueuePayload) -> None:
        if self.fail_next:
            self.fail_next = False
            raise ConnectionError("入队失败（测试注入）")
        self.enqueued.append(payload)

    async def dequeue(self) -> QueuePayload | None:
        return self.enqueued.pop(0) if self.enqueued else None


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


class FakeBudgetRepository(BudgetRepository):
    def __init__(self, quota: BudgetQuota | None = None) -> None:
        self.quota = quota
        self.upserts = 0

    async def get_for_channel(self, channel_instance_id: str) -> BudgetQuota | None:
        return self.quota

    async def upsert(self, quota: BudgetQuota) -> None:
        self.upserts += 1
        self.quota = quota


class FakeMessagePublisher(MessagePublisher):
    def __init__(self) -> None:
        self.replies: list[tuple[ReplyTarget, str]] = []

    async def reply(self, target: ReplyTarget, text: str) -> None:
        self.replies.append((target, text))


class FakeAuditRepository(AuditRepository):
    def __init__(self) -> None:
        self.logs: list[AuditLog] = []

    async def append(self, log: AuditLog) -> None:
        self.logs.append(log)

    async def list_by_channel(self, channel_instance_id: str, limit: int = 100) -> list[AuditLog]:
        out = [x for x in self.logs if x.channel_instance_id == channel_instance_id]
        return out[-limit:]
