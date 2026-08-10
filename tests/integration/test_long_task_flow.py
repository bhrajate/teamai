"""长任务链路端到端：web 进程入队 → worker 消费 → 回帖。

与两侧的单测互补：test_router.py 只验证「入队了」，test_worker.py 只验证
「消费得动」，两者各自用替身封住了对面。本文件把真 MessageRouter、真
TaskOrchestrator、真 worker.handle_payload 串在一个共享队列上，验证的是
中间那道接缝 —— 载荷字段能否被对面正确取用、状态能否接力推进。

仍不连外部服务：队列/仓储/发布器都是内存替身，故可在 CI 无依赖运行。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.worker.main import handle_payload, run_worker
from teamai.application.orchestrator import TaskOrchestrator
from teamai.application.router import MessageRouter
from teamai.domain.models import ChannelInstance, TaskStatus
from teamai.domain.ports import ReplyTarget
from teamai.domain.services import AuditLogWriter
from tests.doubles import (
    FakeChannels,
    FakeIntentClassifier,
    FakeMemory,
    FakePolicyRepo,
    FakeRuntime,
    FakeTags,
    mention,
)
from tests.fakes import (
    FakeAuditRepository,
    FakeMessagePublisher,
    FakeTaskQueue,
    FakeTaskRepository,
)


class Rig:
    """把 web 侧与 worker 侧装在同一套内存依赖上。

    队列、任务仓储、runtime 三者共享是关键：共享队列才能让 web 的 rpush 被
    worker 的 lpop 取到；共享仓储才能验证状态接力；共享 runtime 才能断言
    「Agent 只在 worker 侧跑过一次」。
    """

    def __init__(self, kind: str = "code_review", instance: ChannelInstance | None = None) -> None:
        self.queue = FakeTaskQueue()
        self.task_repo = FakeTaskRepository()
        self.audit_repo = FakeAuditRepository()
        self.publisher = FakeMessagePublisher()
        self.runtime = FakeRuntime()
        # 两侧拿同一个 instance：web 侧用它建任务，worker 侧用它拼 ReplyTarget。
        channels = FakeChannels(instance)

        self.orchestrator = TaskOrchestrator(
            self.task_repo, AuditLogWriter(self.audit_repo), self.queue
        )
        self.router = MessageRouter(
            orchestrator=self.orchestrator,
            intent=FakeIntentClassifier(kind),
            tags=FakeTags(),
            memory=FakeMemory(),
            budget=object(),
            runtime=self.runtime,
            channels=channels,
            policy_repo=FakePolicyRepo(),
        )
        # worker 只认 Container 上的这几个属性，用 SimpleNamespace 足够，
        # 无需为测试造一个真 Container（那会拖进 Redis/Postgres 客户端）。
        self.container = SimpleNamespace(
            task_repo=self.task_repo,
            channels=channels,
            orchestrator=self.orchestrator,
            router=self.router,
            queue=self.queue,
            publisher=self.publisher,
        )

    @property
    def task(self):  # type: ignore[no-untyped-def]
        return next(iter(self.task_repo.items.values()))


async def test_全链路_入队后由worker执行并回帖() -> None:
    rig = Rig()

    # ① web 进程：只入队，当场不执行
    decision = await rig.router.route(mention("帮我审查 payment 模块"))
    assert "已受理" in decision.message
    assert rig.runtime.runs == 0, "web 进程不该跑 Agent"
    assert rig.task.status is TaskStatus.PENDING
    assert len(rig.queue.enqueued) == 1

    # ② worker 进程：取出并执行
    payload = await rig.queue.dequeue()
    assert payload is not None
    await handle_payload(rig.container, payload)

    # ③ 结果：Agent 跑过一次、任务终态、回帖落到正确的线程
    assert rig.runtime.runs == 1
    assert rig.runtime.prompts == ["帮我审查 payment 模块"], "原始指令须跨进程传到 Agent"
    assert rig.task.status is TaskStatus.DONE
    assert rig.publisher.replies == [
        (ReplyTarget(platform="slack", channel_id="C1", thread_ref="1700000000.1"), "执行完毕")
    ]


async def test_全链路_经消费循环跑通(monkeypatch: pytest.MonkeyPatch) -> None:
    """不手工 dequeue，走 run_worker 的真实消费路径。"""
    rig = Rig()
    await rig.router.route(mention())

    import app.worker.main as W

    monkeypatch.setattr(W, "DEQUEUE_BLOCK_SECONDS", 0.01)
    monkeypatch.setattr(W, "ERROR_RETRY_SECONDS", 0.01)

    stop = asyncio.Event()
    runner = asyncio.create_task(run_worker(rig.container, stop))
    await asyncio.sleep(0.05)
    stop.set()
    await asyncio.wait_for(runner, timeout=1.0)

    assert rig.runtime.runs == 1
    assert rig.task.status is TaskStatus.DONE
    assert len(rig.publisher.replies) == 1


async def test_全链路_状态接力为PENDING到RUNNING到DONE() -> None:
    """状态由两个进程接力推进，审计里应留下完整轨迹。"""
    rig = Rig()
    await rig.router.route(mention())
    assert rig.task.status is TaskStatus.PENDING

    payload = await rig.queue.dequeue()
    await handle_payload(rig.container, payload)

    transitions = [
        log.detail.get("to") for log in rig.audit_repo.logs if log.detail.get("to") is not None
    ]
    assert transitions == ["RUNNING", "DONE"], f"状态轨迹不完整: {transitions}"


async def test_全链路_短任务不经队列() -> None:
    """对照组：短任务应在 web 进程直接跑完，队列与 publisher 都不参与。"""
    rig = Rig(kind="query")

    decision = await rig.router.route(mention("现在几点"))

    assert rig.queue.enqueued == []
    assert rig.runtime.runs == 1
    assert rig.task.status is TaskStatus.DONE
    assert decision.message == "执行完毕"
    assert rig.publisher.replies == [], "同步链路由平台适配器直接回复，不经 publisher"


async def test_全链路_worker侧重复消费不重复执行() -> None:
    """幂等：同一载荷被重投（Redis 重试/多副本）时，终态任务应被跳过。"""
    rig = Rig()
    await rig.router.route(mention())
    payload = await rig.queue.dequeue()

    await handle_payload(rig.container, payload)
    await handle_payload(rig.container, payload)

    assert rig.runtime.runs == 1, "重投不该让 Agent 跑第二次"
    assert len(rig.publisher.replies) == 1


async def test_全链路_队列不可用时降级但仍完成() -> None:
    """Redis 挂掉：web 进程就地执行，用户仍拿到结果，只是响应变慢。"""
    rig = Rig()
    rig.queue.fail_next = True

    decision = await rig.router.route(mention())

    assert rig.queue.enqueued == []
    assert rig.runtime.runs == 1
    assert rig.task.status is TaskStatus.DONE
    assert decision.message == "执行完毕"


@pytest.mark.parametrize("platform,channel", [("slack", "C1"), ("feishu", "oc_1")])
async def test_全链路_回帖平台随频道实例(platform: str, channel: str) -> None:
    """ReplyTarget 由 instance 拼出，故换平台无需改队列载荷。"""
    rig = Rig(
        instance=ChannelInstance(
            id="ch_1",
            platform=platform,
            channel_id=channel,
            workspace_id="T1",
            agent_identity="ai_1",
        )
    )

    await rig.router.route(mention())
    payload = await rig.queue.dequeue()
    await handle_payload(rig.container, payload)

    target, _ = rig.publisher.replies[0]
    assert target.platform == platform
    assert target.channel_id == channel
