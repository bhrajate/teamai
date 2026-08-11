"""频道实例服务：Slack 频道到内部实例的映射与复用。"""

from __future__ import annotations

from teamai.domain.identity import gen_id
from teamai.domain.models import ChannelInstance
from teamai.domain.models.audit import AuditAction
from teamai.domain.repositories import ChannelRepository, PolicyRepository
from teamai.domain.services import AuditLogWriter


class ChannelService:
    def __init__(
        self,
        channel_repo: ChannelRepository,
        policy_repo: PolicyRepository,
        audit: AuditLogWriter | None = None,
    ) -> None:
        self._channel_repo = channel_repo
        self._policy_repo = policy_repo
        self._audit = audit

    async def get_or_create(self, platform: str, channel_id: str, workspace_id: str) -> ChannelInstance:
        instance = await self._channel_repo.get_by_platform_channel(platform, channel_id, workspace_id)
        if instance is not None:
            return instance
        instance = ChannelInstance(
            id=gen_id("ch"),
            platform=platform,
            channel_id=channel_id,
            workspace_id=workspace_id,
            agent_identity=gen_id("ai"),
        )
        await self._channel_repo.upsert(instance)
        return instance

    async def get(self, channel_instance_id: str) -> ChannelInstance | None:
        return await self._channel_repo.get(channel_instance_id)

    async def list(self) -> list[ChannelInstance]:
        return await self._channel_repo.list()

    async def update_settings(
        self,
        channel_instance_id: str,
        *,
        ambient_enabled: bool | None = None,
        cross_channel_learning: bool | None = None,
        actor: str | None = None,
    ) -> ChannelInstance | None:
        """改频道级开关。传 None 的字段不动，便于前端做单开关 PATCH。

        两个开关都会放大 agent 的行为半径（主动介入、跨频道读记忆），故一律留痕。
        复用 POLICY_CHANGE 而不新增 AuditAction：action 列是 Postgres 原生 ENUM，
        加值要写 ALTER TYPE 迁移，而这两个开关本就是频道策略的一部分。
        """
        instance = await self._channel_repo.get(channel_instance_id)
        if instance is None:
            return None

        changed: dict[str, bool] = {}
        if ambient_enabled is not None and ambient_enabled != instance.ambient_enabled:
            instance.ambient_enabled = ambient_enabled
            changed["ambient_enabled"] = ambient_enabled
        if cross_channel_learning is not None and cross_channel_learning != instance.cross_channel_learning:
            instance.cross_channel_learning = cross_channel_learning
            changed["cross_channel_learning"] = cross_channel_learning

        if not changed:
            return instance

        await self._channel_repo.upsert(instance)
        if self._audit is not None:
            await self._audit.record(
                channel_instance_id,
                AuditAction.POLICY_CHANGE,
                user_id=actor,
                detail={"channel_settings": changed},
            )
        return instance
