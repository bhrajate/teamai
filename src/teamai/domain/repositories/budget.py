"""预算配额仓储抽象。"""

from __future__ import annotations

from abc import ABC, abstractmethod

from teamai.domain.models.budget import BudgetQuota


class BudgetRepository(ABC):
    @abstractmethod
    async def get_for_channel(self, channel_instance_id: str) -> BudgetQuota | None: ...

    @abstractmethod
    async def list_all(self) -> list[BudgetQuota]:
        """列出全部配额，供周期重置巡检遍历。

        不分页：配额是「每频道一条」的量级，巡检又是低频后台任务，全量取回
        比游标翻页简单。若频道数涨到让这条查询变慢，该加的是按 period_started_at
        过滤的条件，而不是分页。
        """
        ...

    @abstractmethod
    async def upsert(self, quota: BudgetQuota) -> None: ...
