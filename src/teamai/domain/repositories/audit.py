"""审计日志仓储抽象。"""

from __future__ import annotations

from abc import ABC, abstractmethod

from teamai.domain.models.audit import AuditLog


class AuditRepository(ABC):
    @abstractmethod
    async def append(self, log: AuditLog) -> None: ...

    @abstractmethod
    async def list_by_channel(self, channel_instance_id: str, limit: int = 100) -> list[AuditLog]: ...
