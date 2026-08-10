"""任务仓储抽象。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from datetime import datetime

from teamai.domain.models.task import Task, TaskStatus


class TaskRepository(ABC):
    @abstractmethod
    async def create(self, task: Task) -> None: ...

    @abstractmethod
    async def update(self, task: Task) -> None: ...

    @abstractmethod
    async def get(self, task_id: str) -> Task | None: ...

    @abstractmethod
    async def list_by_channel(self, channel_instance_id: str, status: TaskStatus | None = None) -> list[Task]: ...

    @abstractmethod
    async def list_stale(self, statuses: Sequence[TaskStatus], before: datetime) -> list[Task]:
        """跨频道列出处于 statuses 之一、且 updated_at 早于 before 的任务。

        供超时巡检用。按 updated_at 而非 created_at 筛：前者随每次状态迁移刷新，
        故「很久没动过」才是卡住的信号 —— 一个正常推进的长任务不会被误判。
        """
        ...
