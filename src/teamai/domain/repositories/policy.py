"""权限策略仓储抽象。"""

from __future__ import annotations

from abc import ABC, abstractmethod

from teamai.domain.models.policy import PermissionPolicy


class PolicyRepository(ABC):
    @abstractmethod
    async def get_for_channel(self, channel_instance_id: str) -> PermissionPolicy | None: ...

    @abstractmethod
    async def upsert(self, policy: PermissionPolicy) -> None: ...
