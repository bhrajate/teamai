"""Agent 交互记录仓储抽象。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime

from teamai.domain.models.interaction import AgentInteraction


class InteractionRepository(ABC):
    @abstractmethod
    async def record(self, interaction: AgentInteraction) -> None: ...

    @abstractmethod
    async def get(self, interaction_id: str) -> AgentInteraction | None: ...

    @abstractmethod
    async def list_by_channel(
        self, channel_instance_id: str, limit: int = 50
    ) -> list[AgentInteraction]:
        """按时间倒序取最近若干条。limit 有默认值且必须生效 —— 这张表是全量
        增长的，无界查询会随使用时长逐步拖慢控制台。"""
        ...

    @abstractmethod
    async def list_by_task(self, task_id: str) -> list[AgentInteraction]: ...

    @abstractmethod
    async def purge_before(self, cutoff: datetime) -> int:
        """删除 created_at 早于 cutoff 的记录，返回删除行数。供保留期清理任务调用。"""
        ...
