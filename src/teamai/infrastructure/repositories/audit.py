"""AuditRepository 的 SQLAlchemy 实现。

detail 在库里是 JSON 字符串，读写在此处转换。
"""

from __future__ import annotations

import json

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from teamai.domain.models.audit import AuditLog
from teamai.domain.repositories.audit import AuditRepository
from teamai.infrastructure.orm.audit import AuditLogModel


def _audit_to_model(a: AuditLog) -> AuditLogModel:
    return AuditLogModel(
        id=a.id,
        channel_instance_id=a.channel_instance_id,
        user_id=a.user_id,
        action=a.action,
        detail=json.dumps(a.detail),
        task_id=a.task_id,
        tokens_consumed=a.tokens_consumed,
        result=a.result,
        ts=a.ts,
    )


def _model_to_audit(m: AuditLogModel) -> AuditLog:
    return AuditLog(
        id=m.id,
        channel_instance_id=m.channel_instance_id,
        user_id=m.user_id,
        action=m.action,
        detail=json.loads(m.detail or "{}"),
        task_id=m.task_id,
        tokens_consumed=m.tokens_consumed,
        result=m.result,
        ts=m.ts,
    )


class SQLAuditRepository(AuditRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def append(self, log: AuditLog) -> None:
        self._session.add(_audit_to_model(log))

    async def list_by_channel(self, channel_instance_id: str, limit: int = 100) -> list[AuditLog]:
        stmt = (
            select(AuditLogModel)
            .where(AuditLogModel.channel_instance_id == channel_instance_id)
            .order_by(AuditLogModel.ts.desc())
            .limit(limit)
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return [_model_to_audit(r) for r in rows]
