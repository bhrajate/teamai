"""仓储抽象接口（依赖倒置：应用层依赖接口，不依赖具体 DB）。"""

from __future__ import annotations

from abc import ABC, abstractmethod

from teamai.domain.audit import AuditLog
from teamai.domain.budget import BudgetQuota
from teamai.domain.channel import ChannelInstance
from teamai.domain.memory import MemoryEntry, Preference
from teamai.domain.policy import PermissionPolicy
from teamai.domain.tag import TagTemplate
from teamai.domain.task import Task, TaskStatus


class TaskRepository(ABC):
    @abstractmethod
    async def create(self, task: Task) -> None: ...

    @abstractmethod
    async def update(self, task: Task) -> None: ...

    @abstractmethod
    async def get(self, task_id: str) -> Task | None: ...

    @abstractmethod
    async def list_by_channel(self, channel_instance_id: str, status: TaskStatus | None = None) -> list[Task]: ...


class MemoryRepository(ABC):
    @abstractmethod
    async def store(self, entry: MemoryEntry) -> None: ...

    @abstractmethod
    async def list_by_channel(self, channel_instance_id: str) -> list[MemoryEntry]: ...

    @abstractmethod
    async def get(self, entry_id: str) -> MemoryEntry | None: ...

    @abstractmethod
    async def delete(self, entry_id: str) -> None: ...

    @abstractmethod
    async def set_preference(self, pref: Preference) -> None: ...

    @abstractmethod
    async def list_preferences(self, channel_instance_id: str) -> list[Preference]: ...


class TagRepository(ABC):
    @abstractmethod
    async def create(self, tag: TagTemplate) -> None: ...

    @abstractmethod
    async def get(self, channel_instance_id: str, name: str) -> TagTemplate | None: ...

    @abstractmethod
    async def list_by_channel(self, channel_instance_id: str) -> list[TagTemplate]: ...

    @abstractmethod
    async def delete(self, tag_id: str) -> None: ...

    @abstractmethod
    async def set_active(self, tag_id: str, active: bool) -> None: ...


class PolicyRepository(ABC):
    @abstractmethod
    async def get_for_channel(self, channel_instance_id: str) -> PermissionPolicy | None: ...

    @abstractmethod
    async def upsert(self, policy: PermissionPolicy) -> None: ...


class BudgetRepository(ABC):
    @abstractmethod
    async def get_for_channel(self, channel_instance_id: str) -> BudgetQuota | None: ...

    @abstractmethod
    async def upsert(self, quota: BudgetQuota) -> None: ...


class ChannelRepository(ABC):
    @abstractmethod
    async def get(self, channel_instance_id: str) -> ChannelInstance | None: ...

    @abstractmethod
    async def get_by_slack(self, channel_id: str, workspace_id: str) -> ChannelInstance | None: ...

    @abstractmethod
    async def upsert(self, instance: ChannelInstance) -> None: ...


class AuditRepository(ABC):
    @abstractmethod
    async def append(self, log: AuditLog) -> None: ...

    @abstractmethod
    async def list_by_channel(self, channel_instance_id: str, limit: int = 100) -> list[AuditLog]: ...
