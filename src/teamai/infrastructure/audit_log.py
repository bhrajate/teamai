"""审计写入组件：面向用例层的便捷封装。"""

from __future__ import annotations

from teamai.domain.audit import AuditAction, AuditLog, AuditResult
from teamai.infrastructure.repositories.interface import AuditRepository
from teamai.util.events import gen_id


class AuditLogWriter:
    def __init__(self, repo: AuditRepository) -> None:
        self._repo = repo

    async def record(
        self,
        channel_instance_id: str,
        action: AuditAction,
        *,
        user_id: str | None = None,
        detail: dict | None = None,
        task_id: str | None = None,
        tokens_consumed: int = 0,
        result: AuditResult = AuditResult.SUCCESS,
    ) -> AuditLog:
        log = AuditLog(
            id=gen_id("audit"),
            channel_instance_id=channel_instance_id,
            user_id=user_id,
            action=action,
            detail=detail or {},
            task_id=task_id,
            tokens_consumed=tokens_consumed,
            result=result,
        )
        await self._repo.append(log)
        return log
