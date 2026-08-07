"""TagRepository 的 SQLAlchemy 实现。"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from teamai.domain.models.tag import TagTemplate
from teamai.domain.repositories.tag import TagRepository
from teamai.infrastructure.orm.tag import TagTemplateModel


def _tag_to_model(t: TagTemplate) -> TagTemplateModel:
    return TagTemplateModel(
        id=t.id,
        channel_instance_id=t.channel_instance_id,
        name=t.name,
        instruction=t.instruction,
        role=t.role,
        output_style=t.output_style,
        shared=t.shared,
        created_by=t.created_by,
        active=t.active,
        created_at=t.created_at,
    )


def _model_to_tag(m: TagTemplateModel) -> TagTemplate:
    return TagTemplate(
        id=m.id,
        channel_instance_id=m.channel_instance_id,
        name=m.name,
        instruction=m.instruction,
        role=m.role,
        output_style=m.output_style,
        shared=m.shared,
        created_by=m.created_by,
        active=m.active,
        created_at=m.created_at,
    )


class SQLTagRepository(TagRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, tag: TagTemplate) -> None:
        self._session.add(_tag_to_model(tag))

    async def get(self, channel_instance_id: str, name: str) -> TagTemplate | None:
        stmt = select(TagTemplateModel).where(
            TagTemplateModel.channel_instance_id == channel_instance_id,
            TagTemplateModel.name == name,
        )
        m = (await self._session.execute(stmt)).scalars().first()
        return _model_to_tag(m) if m else None

    async def list_by_channel(self, channel_instance_id: str) -> list[TagTemplate]:
        stmt = select(TagTemplateModel).where(TagTemplateModel.channel_instance_id == channel_instance_id)
        rows = (await self._session.execute(stmt)).scalars().all()
        return [_model_to_tag(r) for r in rows]

    async def delete(self, tag_id: str) -> None:
        m = await self._session.get(TagTemplateModel, tag_id)
        if m:
            await self._session.delete(m)

    async def set_active(self, tag_id: str, active: bool) -> None:
        m = await self._session.get(TagTemplateModel, tag_id)
        if m:
            m.active = active
