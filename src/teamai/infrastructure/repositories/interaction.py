"""InteractionRepository 的 SQLAlchemy 实现。"""

from __future__ import annotations

import json
import logging
from datetime import datetime

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from teamai.domain.models.interaction import AgentInteraction, InteractionResult
from teamai.domain.repositories.interaction import InteractionRepository
from teamai.infrastructure.orm.interaction import AgentInteractionModel

logger = logging.getLogger(__name__)


def _to_model(i: AgentInteraction) -> AgentInteractionModel:
    return AgentInteractionModel(
        id=i.id,
        task_id=i.task_id,
        channel_instance_id=i.channel_instance_id,
        thread_ref=i.thread_ref,
        requester_id=i.requester_id,
        user_prompt=i.user_prompt,
        system_prompt=i.system_prompt,
        context_refs=json.dumps(i.context_refs, ensure_ascii=False),
        model_level=i.model_level,
        model_id=i.model_id,
        response=i.response,
        tokens_in=i.tokens_in,
        tokens_out=i.tokens_out,
        result=i.result,
        error=i.error,
        created_at=i.created_at,
    )


def _to_domain(m: AgentInteractionModel) -> AgentInteraction:
    try:
        refs = json.loads(m.context_refs or "{}")
    except json.JSONDecodeError:
        # 坏 JSON 不该让整条记录读不出来：其余字段（提示词、响应、token）
        # 仍有价值，引用关系降级为空即可。
        logger.warning(f"交互记录 {m.id} 的 context_refs 不是合法 JSON，按空处理")
        refs = {}
    return AgentInteraction(
        id=m.id,
        task_id=m.task_id,
        channel_instance_id=m.channel_instance_id,
        thread_ref=m.thread_ref,
        requester_id=m.requester_id,
        user_prompt=m.user_prompt,
        system_prompt=m.system_prompt,
        model_level=m.model_level,
        model_id=m.model_id,
        response=m.response,
        tokens_in=m.tokens_in,
        tokens_out=m.tokens_out,
        result=m.result if isinstance(m.result, InteractionResult) else InteractionResult(m.result),
        error=m.error,
        context_refs=refs,
        created_at=m.created_at,
    )


class SQLInteractionRepository(InteractionRepository):
    """提交理由见 SQLTaskRepository 的类说明（共享 session 下每次操作自行提交）。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def record(self, interaction: AgentInteraction) -> None:
        self._session.add(_to_model(interaction))
        await self._session.commit()

    async def get(self, interaction_id: str) -> AgentInteraction | None:
        m = await self._session.get(AgentInteractionModel, interaction_id)
        return _to_domain(m) if m else None

    async def list_by_channel(
        self, channel_instance_id: str, limit: int = 50
    ) -> list[AgentInteraction]:
        stmt = (
            select(AgentInteractionModel)
            .where(AgentInteractionModel.channel_instance_id == channel_instance_id)
            .order_by(AgentInteractionModel.created_at.desc())
            .limit(limit)
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return [_to_domain(r) for r in rows]

    async def list_by_task(self, task_id: str) -> list[AgentInteraction]:
        """按时间正序：同一任务的多次调用要能顺着读下来（重试、多阶段）。"""
        stmt = (
            select(AgentInteractionModel)
            .where(AgentInteractionModel.task_id == task_id)
            .order_by(AgentInteractionModel.created_at.asc())
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return [_to_domain(r) for r in rows]

    async def purge_before(self, cutoff: datetime) -> int:
        stmt = delete(AgentInteractionModel).where(AgentInteractionModel.created_at < cutoff)
        result = await self._session.execute(stmt)
        await self._session.commit()
        return int(result.rowcount or 0)
