"""标签解析：对话标签模板的创建、激活、解析。"""

from __future__ import annotations

from teamai.domain.identity import gen_id
from teamai.domain.models import AuditAction, TagTemplate
from teamai.domain.repositories import TagRepository
from teamai.domain.services import AuditLogWriter


class TagResolver:
    def __init__(self, repo: TagRepository, audit: AuditLogWriter) -> None:
        self._repo = repo
        self._audit = audit

    async def create(
        self,
        channel_instance_id: str,
        name: str,
        instruction: str,
        *,
        role: str | None = None,
        output_style: str | None = None,
        created_by: str | None = None,
    ) -> TagTemplate:
        tag = TagTemplate(
            id=gen_id("tag"),
            channel_instance_id=channel_instance_id,
            name=name,
            instruction=instruction,
            role=role,
            output_style=output_style,
            created_by=created_by,
        )
        await self._repo.create(tag)
        await self._audit.record(
            channel_instance_id,
            AuditAction.POLICY_CHANGE,
            user_id=created_by,
            detail={"event": "tag_create", "tag": name},
        )
        return tag

    async def resolve(self, channel_instance_id: str, name: str) -> TagTemplate | None:
        """解析标签；仅返回 active 状态的标签。"""
        tag = await self._repo.get(channel_instance_id, name)
        if tag is not None and tag.active:
            return tag
        return None

    async def list(self, channel_instance_id: str) -> list[TagTemplate]:
        return await self._repo.list_by_channel(channel_instance_id)

    async def delete(self, channel_instance_id: str, tag_id: str, actor: str | None = None) -> None:
        await self._repo.delete(tag_id)
        await self._audit.record(
            channel_instance_id,
            AuditAction.POLICY_CHANGE,
            user_id=actor,
            detail={"event": "tag_delete", "tag_id": tag_id},
        )

    async def set_active(self, tag_id: str, active: bool) -> None:
        await self._repo.set_active(tag_id, active)
