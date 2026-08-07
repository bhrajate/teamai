"""审计写入领域服务。

application 与 agent 两层都需要留痕，故放在 domain 层避免两者互相依赖。
仅依赖 domain 模型与 AuditRepository 抽象，无 I/O。
"""

from __future__ import annotations

from teamai.domain.models.audit import AuditAction, AuditLog, AuditResult
from teamai.domain.repositories.audit import AuditRepository
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
