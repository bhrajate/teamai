"""Ambient Mode 巡检：两级开关、阈值、冷却、失败隔离。

重点验「不该打扰时确实不打扰」—— 主动介入的误报比漏报更伤：
群里被无故 at 几次，管理员就会把总闸关掉，功能等于没有。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pytest

from teamai.application.ambient import DEFAULT_IDLE_MINUTES, AmbientService
from teamai.domain.models import Task, TaskStatus
from teamai.domain.models.channel import ChannelInstance
from teamai.domain.models.policy import AmbientRule, PermissionPolicy
from teamai.domain.ports import ReplyTarget
from teamai.infrastructure.cooldown import InMemoryAmbientCooldown
from tests.fakes import FakeMessagePublisher, FakeTaskRepository

CH = "ci_1"


@dataclass
class FakeChannelRepo:
    """按 id 返回，与共享的 FakeChannels 不同 —— 多频道用例要区分。"""

    items: dict[str, ChannelInstance] = field(default_factory=dict)

    async def get(self, channel_instance_id: str) -> ChannelInstance | None:
        return self.items.get(channel_instance_id)

    async def get_by_platform_channel(self, platform, channel_id, workspace_id):  # noqa: ANN001, ANN201
        return None

    async def upsert(self, instance: ChannelInstance) -> None:
        self.items[instance.id] = instance


@dataclass
class FakePolicies:
    items: dict[str, PermissionPolicy] = field(default_factory=dict)

    async def get_for_channel(self, channel_instance_id: str) -> PermissionPolicy | None:
        return self.items.get(channel_instance_id)

    async def upsert(self, policy: PermissionPolicy) -> None:
        self.items[policy.channel_instance_id] = policy


def _instance(*, ambient: bool, cid: str = CH, platform: str = "slack") -> ChannelInstance:
    return ChannelInstance(
        id=cid,
        platform=platform,
        channel_id=f"C_{cid}",
        workspace_id="T1",
        agent_identity="teamai",
        ambient_enabled=ambient,
    )


def _policy(*rules: AmbientRule, cid: str = CH) -> PermissionPolicy:
    return PermissionPolicy(id=f"pol_{cid}", channel_instance_id=cid, ambient_rules=list(rules))


def _task(
    *, idle_minutes: int, status: TaskStatus = TaskStatus.RUNNING, tid: str = "tk_1", cid: str = CH
) -> Task:
    now = datetime.now(UTC)
    return Task(
        id=tid,
        channel_instance_id=cid,
        thread_ref="th_1",
        requester_id="U1",
        intent="code_review",
        status=status,
        created_at=now - timedelta(minutes=idle_minutes + 5),
        updated_at=now - timedelta(minutes=idle_minutes),
    )


def _build(
    tasks: list[Task],
    instances: list[ChannelInstance],
    policies: list[PermissionPolicy],
) -> tuple[AmbientService, FakeMessagePublisher]:
    repo = FakeTaskRepository()
    for t in tasks:
        repo.items[t.id] = t
    publisher = FakeMessagePublisher()
    service = AmbientService(
        tasks=repo,
        channels=FakeChannelRepo({i.id: i for i in instances}),
        policies=FakePolicies({p.channel_instance_id: p for p in policies}),
        cooldown=InMemoryAmbientCooldown(),
        publisher=publisher,
    )
    return service, publisher


STALE = DEFAULT_IDLE_MINUTES + 10
RULE = AmbientRule(trigger="thread_stale")


class Test两级开关:
    async def test_频道开且规则配了才提醒(self) -> None:
        service, pub = _build([_task(idle_minutes=STALE)], [_instance(ambient=True)], [_policy(RULE)])
        report = await service.sweep()
        assert report.nudged == ["tk_1"]
        assert len(pub.replies) == 1

    async def test_总闸关则不提醒(self) -> None:
        """规则配好了也不该发 —— 频道级开关是管理员的最终否决权。"""
        service, pub = _build([_task(idle_minutes=STALE)], [_instance(ambient=False)], [_policy(RULE)])
        report = await service.sweep()
        assert report.considered == 0
        assert pub.replies == []

    async def test_无规则则不提醒(self) -> None:
        service, pub = _build([_task(idle_minutes=STALE)], [_instance(ambient=True)], [_policy()])
        assert (await service.sweep()).considered == 0
        assert pub.replies == []

    async def test_无策略记录则不提醒(self) -> None:
        service, pub = _build([_task(idle_minutes=STALE)], [_instance(ambient=True)], [])
        assert (await service.sweep()).considered == 0
        assert pub.replies == []

    async def test_频道不存在则跳过(self) -> None:
        service, pub = _build([_task(idle_minutes=STALE)], [], [_policy(RULE)])
        assert (await service.sweep()).considered == 0
        assert pub.replies == []


class Test阈值:
    async def test_未达沉寂阈值不提醒(self) -> None:
        service, pub = _build([_task(idle_minutes=1)], [_instance(ambient=True)], [_policy(RULE)])
        assert (await service.sweep()).considered == 0
        assert pub.replies == []

    async def test_规则可配更严的阈值(self) -> None:
        """频道阈值比查询窗口更严时，窗口内但未达该频道阈值的任务不该被催。"""
        rule = AmbientRule(trigger="thread_stale", params={"idle_minutes": STALE + 100})
        service, pub = _build([_task(idle_minutes=STALE)], [_instance(ambient=True)], [_policy(rule)])
        assert (await service.sweep()).considered == 0
        assert pub.replies == []

    @pytest.mark.parametrize("bad", [0, -5, "abc", None])
    async def test_非法参数回落默认值(self, bad: object) -> None:
        """策略经 Admin API 写入，一个笔误不该让整轮巡检崩掉或静默失效。"""
        rule = AmbientRule(trigger="thread_stale", params={"idle_minutes": bad})
        service, pub = _build([_task(idle_minutes=STALE)], [_instance(ambient=True)], [_policy(rule)])
        assert (await service.sweep()).nudged == ["tk_1"]
        assert len(pub.replies) == 1


class Test状态过滤:
    @pytest.mark.parametrize("status", [TaskStatus.RUNNING, TaskStatus.WAITING_INPUT])
    async def test_进行中的状态会被催(self, status: TaskStatus) -> None:
        service, _ = _build(
            [_task(idle_minutes=STALE, status=status)], [_instance(ambient=True)], [_policy(RULE)]
        )
        assert (await service.sweep()).nudged == ["tk_1"]

    @pytest.mark.parametrize(
        "status",
        [TaskStatus.DONE, TaskStatus.FAILED, TaskStatus.CANCELLED, TaskStatus.PENDING, TaskStatus.PAUSED],
    )
    async def test_终态与暂停不催(self, status: TaskStatus) -> None:
        """PAUSED 是预算耗尽，催也推不动；PENDING 归超时巡检管。"""
        service, pub = _build(
            [_task(idle_minutes=STALE, status=status)], [_instance(ambient=True)], [_policy(RULE)]
        )
        assert (await service.sweep()).considered == 0
        assert pub.replies == []


class Test冷却:
    async def test_同一任务不会连着催两次(self) -> None:
        """巡检每 N 分钟跑一轮，没有冷却就会每轮都催同一个任务。"""
        service, pub = _build([_task(idle_minutes=STALE)], [_instance(ambient=True)], [_policy(RULE)])
        first = await service.sweep()
        second = await service.sweep()
        assert first.nudged == ["tk_1"]
        assert second.nudged == []
        assert second.cooled == ["tk_1"]
        assert len(pub.replies) == 1

    async def test_冷却按任务隔离(self) -> None:
        tasks = [_task(idle_minutes=STALE, tid="tk_1"), _task(idle_minutes=STALE, tid="tk_2")]
        service, pub = _build(tasks, [_instance(ambient=True)], [_policy(RULE)])
        assert sorted((await service.sweep()).nudged) == ["tk_1", "tk_2"]
        assert len(pub.replies) == 2

    async def test_冷却时长可配(self) -> None:
        """配 0 分钟等于不冷却 —— 但 0 属非法值，应回落到默认（即仍冷却）。"""
        rule = AmbientRule(trigger="thread_stale", params={"cooldown_minutes": 0})
        service, _ = _build([_task(idle_minutes=STALE)], [_instance(ambient=True)], [_policy(rule)])
        await service.sweep()
        assert (await service.sweep()).cooled == ["tk_1"]


class Test失败隔离:
    async def test_发送失败记入failed而非nudged(self) -> None:
        service, pub = _build([_task(idle_minutes=STALE)], [_instance(ambient=True)], [_policy(RULE)])

        async def boom(target: ReplyTarget, text: str) -> None:
            raise ConnectionError("平台不可达")

        pub.reply = boom  # type: ignore[method-assign]
        report = await service.sweep()
        assert report.failed == ["tk_1"]
        assert report.nudged == []

    async def test_发送失败后不在本轮重试(self) -> None:
        """冷却已占位：平台故障时不该每轮对同一任务重试。"""
        service, pub = _build([_task(idle_minutes=STALE)], [_instance(ambient=True)], [_policy(RULE)])
        calls = 0

        async def boom(target: ReplyTarget, text: str) -> None:
            nonlocal calls
            calls += 1
            raise ConnectionError("平台不可达")

        pub.reply = boom  # type: ignore[method-assign]
        await service.sweep()
        assert (await service.sweep()).cooled == ["tk_1"]
        assert calls == 1

    async def test_一个频道失败不影响其他频道(self) -> None:
        """策略读取抛错时，其他频道的提醒仍要发出去。"""
        tasks = [
            _task(idle_minutes=STALE, tid="tk_bad", cid="ci_bad"),
            _task(idle_minutes=STALE, tid="tk_ok", cid="ci_ok"),
        ]
        instances = [_instance(ambient=True, cid="ci_bad"), _instance(ambient=True, cid="ci_ok")]
        policies = FakePolicies({p.channel_instance_id: p for p in (_policy(RULE, cid="ci_ok"),)})

        async def flaky(channel_instance_id: str) -> PermissionPolicy | None:
            if channel_instance_id == "ci_bad":
                raise RuntimeError("策略表读取失败")
            return policies.items.get(channel_instance_id)

        repo = FakeTaskRepository()
        for t in tasks:
            repo.items[t.id] = t
        policies.get_for_channel = flaky  # type: ignore[method-assign]
        pub = FakeMessagePublisher()
        service = AmbientService(
            tasks=repo,
            channels=FakeChannelRepo({i.id: i for i in instances}),
            policies=policies,
            cooldown=InMemoryAmbientCooldown(),
            publisher=pub,
        )
        assert (await service.sweep()).nudged == ["tk_ok"]


class Test提醒内容:
    async def test_回到任务所在线程(self) -> None:
        service, pub = _build([_task(idle_minutes=STALE)], [_instance(ambient=True)], [_policy(RULE)])
        await service.sweep()
        target, _ = pub.replies[0]
        assert (target.platform, target.channel_id, target.thread_ref) == ("slack", f"C_{CH}", "th_1")

    async def test_按平台分发(self) -> None:
        service, pub = _build(
            [_task(idle_minutes=STALE)], [_instance(ambient=True, platform="feishu")], [_policy(RULE)]
        )
        await service.sweep()
        assert pub.replies[0][0].platform == "feishu"

    async def test_等输入与执行中措辞不同(self) -> None:
        """两种停滞的下一步动作不同：一个要人补信息，一个要人决定是否继续。"""
        texts = {}
        for status in (TaskStatus.RUNNING, TaskStatus.WAITING_INPUT):
            service, pub = _build(
                [_task(idle_minutes=STALE, status=status)], [_instance(ambient=True)], [_policy(RULE)]
            )
            await service.sweep()
            texts[status] = pub.replies[0][1]
        assert texts[TaskStatus.WAITING_INPUT] != texts[TaskStatus.RUNNING]
        assert "补充信息" in texts[TaskStatus.WAITING_INPUT]

    async def test_未实现的规则只告警不崩(self) -> None:
        """error_spike / deploy_status 尚未实现，配了也不该让巡检失败。"""
        rules = (AmbientRule(trigger="error_spike"), RULE)
        service, pub = _build([_task(idle_minutes=STALE)], [_instance(ambient=True)], [_policy(*rules)])
        assert (await service.sweep()).nudged == ["tk_1"]
