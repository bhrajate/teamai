"""PolicyRepository 的 SQLAlchemy 实现。

allowed_tools / ambient_rules 在库里是 JSON 数组字符串，读写在此处转换。
"""

from __future__ import annotations

import json

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from teamai.domain.models.policy import AmbientRule, PermissionPolicy
from teamai.domain.repositories.policy import PolicyRepository
from teamai.infrastructure.orm.policy import PolicyModel


def _policy_to_model(p: PermissionPolicy) -> PolicyModel:
    return PolicyModel(
        id=p.id,
        channel_instance_id=p.channel_instance_id,
        allowed_tools=json.dumps(p.allowed_tools),
        ambient_rules=json.dumps([r.__dict__ for r in p.ambient_rules]),
        updated_by=p.updated_by,
        updated_at=p.updated_at,
    )


def _model_to_policy(m: PolicyModel) -> PermissionPolicy:
    tools = json.loads(m.allowed_tools or "[]")
    rules = [
        AmbientRule(trigger=r.get("trigger", ""), params=r.get("params", {}), action=r.get("action", "nudge"))
        for r in json.loads(m.ambient_rules or "[]")
    ]
    return PermissionPolicy(
        id=m.id,
        channel_instance_id=m.channel_instance_id,
        allowed_tools=tools,
        ambient_rules=rules,
        updated_by=m.updated_by,
        updated_at=m.updated_at,
    )


class SQLPolicyRepository(PolicyRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_for_channel(self, channel_instance_id: str) -> PermissionPolicy | None:
        stmt = select(PolicyModel).where(PolicyModel.channel_instance_id == channel_instance_id)
        m = (await self._session.execute(stmt)).scalars().first()
        return _model_to_policy(m) if m else None

    async def upsert(self, policy: PermissionPolicy) -> None:
        await self._session.merge(_policy_to_model(policy))
