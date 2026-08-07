"""预算配额仓储抽象。"""

from __future__ import annotations

from abc import ABC, abstractmethod

from teamai.domain.models.budget import BudgetQuota


class BudgetRepository(ABC):
    @abstractmethod
    async def get_for_channel(self, channel_instance_id: str) -> BudgetQuota | None: ...

    @abstractmethod
    async def upsert(self, quota: BudgetQuota) -> None: ...
