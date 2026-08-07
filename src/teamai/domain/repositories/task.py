"""任务仓储抽象。"""

from __future__ import annotations

from abc import ABC, abstractmethod

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
