"""频道实例服务：列举、开关变更与留痕。

两个开关都会放大 agent 的行为半径 —— ambient_enabled 让它主动开口，
cross_channel_learning 让它读别的频道的记忆。故重点验「改了一定留痕」
与「没改不留痕」：审计噪音会把真正的变更淹掉。
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from teamai.application.channel import ChannelService
from teamai.domain.models import ChannelInstance
from teamai.domain.models.audit import AuditAction
from teamai.domain.services import AuditLogWriter
from tests.fakes import FakeAuditRepository


@dataclass
class FakeChannelRepo:
    items: dict[str, ChannelInstance] = field(default_factory=dict)

    async def get(self, channel_instance_id: str) -> ChannelInstance | None:
        return self.items.get(channel_instance_id)

    async def get_by_platform_channel(self, platform, channel_id, workspace_id):  # noqa: ANN001, ANN201
        return next(
            (
                c
                for c in self.items.values()
                if c.platform == platform and c.channel_id == channel_id and c.workspace_id == workspace_id
            ),
            None,
        )

    async def upsert(self, instance: ChannelInstance) -> None:
        self.items[instance.id] = instance

    async def list(self) -> list[ChannelInstance]:
        return sorted(self.items.values(), key=lambda c: c.id, reverse=True)


@dataclass
class FakePolicyRepo:
    async def get_for_channel(self, channel_instance_id: str):  # noqa: ANN201
        return None

    async def upsert(self, policy) -> None:  # noqa: ANN001
        return None


def _instance(cid: str, **kw) -> ChannelInstance:  # noqa: ANN003
    return ChannelInstance(
        id=cid,
        platform=kw.pop("platform", "slack"),
        channel_id=kw.pop("channel_id", f"C{cid}"),
        workspace_id=kw.pop("workspace_id", "W1"),
        agent_identity=kw.pop("agent_identity", f"ai_{cid}"),
        **kw,
    )


@pytest.fixture
def repo() -> FakeChannelRepo:
    return FakeChannelRepo(items={"ch_1": _instance("ch_1"), "ch_2": _instance("ch_2")})


@pytest.fixture
def audit_repo() -> FakeAuditRepository:
    return FakeAuditRepository()


@pytest.fixture
def service(repo: FakeChannelRepo, audit_repo: FakeAuditRepository) -> ChannelService:
    return ChannelService(repo, FakePolicyRepo(), AuditLogWriter(audit_repo))


async def test_列举按id倒序(service: ChannelService) -> None:
    """id 是 `ch_<ULID>`，字典序即时间序，故倒序等价于「最近创建的在前」。"""
    assert [c.id for c in await service.list()] == ["ch_2", "ch_1"]


async def test_改开关落盘并留痕(
    service: ChannelService, repo: FakeChannelRepo, audit_repo: FakeAuditRepository
) -> None:
    out = await service.update_settings("ch_1", ambient_enabled=True, actor="U1")

    assert out is not None and out.ambient_enabled is True
    assert repo.items["ch_1"].ambient_enabled is True

    assert len(audit_repo.logs) == 1
    log = audit_repo.logs[0]
    assert log.action is AuditAction.POLICY_CHANGE
    assert log.user_id == "U1"
    assert log.detail == {"channel_settings": {"ambient_enabled": True}}


async def test_缺省字段不动(service: ChannelService, repo: FakeChannelRepo) -> None:
    """前端做单开关 PATCH，未提交的字段必须原样保留。"""
    await service.update_settings("ch_1", cross_channel_learning=True)
    await service.update_settings("ch_1", ambient_enabled=True)

    assert repo.items["ch_1"].cross_channel_learning is True
    assert repo.items["ch_1"].ambient_enabled is True


async def test_值没变则不留痕(service: ChannelService, audit_repo: FakeAuditRepository) -> None:
    """重复提交同一个值不该刷审计 —— 噪音会把真正的变更淹掉。"""
    await service.update_settings("ch_1", ambient_enabled=True)
    await service.update_settings("ch_1", ambient_enabled=True)

    assert len(audit_repo.logs) == 1


async def test_全缺省时不留痕(service: ChannelService, audit_repo: FakeAuditRepository) -> None:
    await service.update_settings("ch_1")
    assert audit_repo.logs == []


async def test_只记实际变更的字段(service: ChannelService, audit_repo: FakeAuditRepository) -> None:
    """两个字段一起提交但只有一个真变了，detail 里就该只有那一个。"""
    await service.update_settings("ch_1", ambient_enabled=True)
    audit_repo.logs.clear()

    await service.update_settings("ch_1", ambient_enabled=True, cross_channel_learning=True)

    assert audit_repo.logs[0].detail == {"channel_settings": {"cross_channel_learning": True}}


async def test_频道不存在返回None(service: ChannelService, audit_repo: FakeAuditRepository) -> None:
    assert await service.update_settings("ch_nope", ambient_enabled=True) is None
    assert audit_repo.logs == []


async def test_关开关也留痕(service: ChannelService, audit_repo: FakeAuditRepository) -> None:
    """False 是有效值，不能被当成「未提交」丢掉 —— 关闭主动介入尤其要留痕。"""
    await service.update_settings("ch_1", ambient_enabled=True)
    audit_repo.logs.clear()

    out = await service.update_settings("ch_1", ambient_enabled=False)

    assert out is not None and out.ambient_enabled is False
    assert audit_repo.logs[0].detail == {"channel_settings": {"ambient_enabled": False}}


async def test_无审计器也能改(repo: FakeChannelRepo) -> None:
    """audit 是可选依赖，缺它不该影响改配置本身。"""
    out = await ChannelService(repo, FakePolicyRepo()).update_settings("ch_1", ambient_enabled=True)
    assert out is not None and out.ambient_enabled is True
