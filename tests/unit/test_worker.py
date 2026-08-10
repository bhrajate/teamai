"""Worker 消费循环与单任务处理的单元测试。

worker 的核心契约是「单个任务出问题不能拖垮整个循环」，所以这里重点覆盖
各类异常载荷（任务不存在、频道不存在、已终态、执行抛异常）都不外抛。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.worker.main import handle_payload, run_worker
from teamai.application.router import RoutingDecision
from teamai.domain.models import ChannelInstance, Task, TaskStatus
from teamai.domain.ports import QueuePayload, ReplyTarget
from tests.fakes import FakeMessagePublisher, FakeTaskQueue, FakeTaskRepository


class FakeChannels:
    def __init__(self, instance: ChannelInstance | None) -> None:
        self._instance = instance

    async def get(self, channel_instance_id: str) -> ChannelInstance | None:
        return self._instance


class FakeOrchestrator:
    def __init__(self) -> None:
        self.transitions: list[TaskStatus] = []

    async def transition(self, task: Task, to: TaskStatus, actor: str) -> Task:
        task.transition(to, actor)
        self.transitions.append(to)
        return task


class FakeRouter:
    def __init__(self, decision: RoutingDecision | None = None, raises: Exception | None = None) -> None:
        self.decision = decision or RoutingDecision(handler="respond", message="完成")
        self.raises = raises
        self.calls: list[tuple] = []

    async def execute_task(self, task, prompt, tag_name, instance, *, actor):  # type: ignore[no-untyped-def]
        self.calls.append((task.id, prompt, tag_name, instance.id, actor))
        if self.raises is not None:
            raise self.raises
        # 真 router 会推进终态，这里同步一下以贴近真实行为
        task.transition(TaskStatus.DONE, actor)
        return self.decision


def _task(status: TaskStatus = TaskStatus.PENDING, **kw) -> Task:
    task = Task(
        id=kw.get("id", "task_1"),
        channel_instance_id=kw.get("channel_instance_id", "ch_1"),
        thread_ref="1.0",
        requester_id="U1",
        intent="qa",
        tag_name=kw.get("tag_name"),
    )
    task.status = status
    return task


def _instance() -> ChannelInstance:
    return ChannelInstance(
        id="ch_1", platform="slack", channel_id="C1", workspace_id="T1", agent_identity="ai_1"
    )


def _container(
    task: Task | None, instance: ChannelInstance | None, router: FakeRouter
) -> SimpleNamespace:
    repo = FakeTaskRepository()
    if task is not None:
        repo.items[task.id] = task
    return SimpleNamespace(
        task_repo=repo,
        channels=FakeChannels(instance),
        orchestrator=FakeOrchestrator(),
        router=router,
        queue=FakeTaskQueue(),
        publisher=FakeMessagePublisher(),
    )


def _payload(**kw) -> QueuePayload:
    return QueuePayload(
        task_id=kw.get("task_id", "task_1"),
        channel_instance_id=kw.get("channel_instance_id", "ch_1"),
        model_level=kw.get("model_level", "light"),
        prompt=kw.get("prompt", "帮我看下这段代码"),
        tag_name=kw.get("tag_name"),
        thread_ref=kw.get("thread_ref", "1.0"),
    )


async def test_正常载荷_推进为RUNNING并执行() -> None:
    task = _task(TaskStatus.PENDING)
    router = FakeRouter()
    c = _container(task, _instance(), router)

    await handle_payload(c, _payload())

    assert c.orchestrator.transitions == [TaskStatus.RUNNING]
    assert len(router.calls) == 1
    assert router.calls[0][1] == "帮我看下这段代码"
    assert task.status is TaskStatus.DONE


async def test_已RUNNING的任务不重复推进状态() -> None:
    """RUNNING→RUNNING 是非法迁移，worker 须跳过这步而非抛异常。"""
    task = _task(TaskStatus.RUNNING)
    router = FakeRouter()
    c = _container(task, _instance(), router)

    await handle_payload(c, _payload())

    assert c.orchestrator.transitions == []
    assert len(router.calls) == 1


@pytest.mark.parametrize("status", [TaskStatus.DONE, TaskStatus.FAILED, TaskStatus.CANCELLED])
async def test_终态任务被跳过(status: TaskStatus) -> None:
    task = _task(status)
    router = FakeRouter()
    c = _container(task, _instance(), router)

    await handle_payload(c, _payload())

    assert router.calls == []
    assert task.status is status


async def test_任务不存在_丢弃且不抛() -> None:
    router = FakeRouter()
    c = _container(None, _instance(), router)

    await handle_payload(c, _payload(task_id="task_missing"))

    assert router.calls == []


async def test_频道实例不存在_丢弃且不抛() -> None:
    task = _task()
    router = FakeRouter()
    c = _container(task, None, router)

    await handle_payload(c, _payload())

    assert router.calls == []
    assert task.status is TaskStatus.PENDING


async def test_执行抛异常_转FAILED且不外抛() -> None:
    task = _task(TaskStatus.PENDING)
    router = FakeRouter(raises=RuntimeError("模型调用失败"))
    c = _container(task, _instance(), router)

    await handle_payload(c, _payload())

    assert task.status is TaskStatus.FAILED
    assert c.orchestrator.transitions == [TaskStatus.RUNNING, TaskStatus.FAILED]


async def test_prompt为空时回退到intent() -> None:
    task = _task()
    router = FakeRouter()
    c = _container(task, _instance(), router)

    await handle_payload(c, _payload(prompt=""))

    assert router.calls[0][1] == "qa"


async def test_长任务完成后经publisher回帖() -> None:
    """异步链路与同步链路共用 MessagePublisher 端口回帖。

    ReplyTarget 由 instance（platform + channel_id）与 task.thread_ref 拼出，
    队列载荷不再需要额外携带平台信息。
    """
    task = _task()
    router = FakeRouter()
    c = _container(task, _instance(), router)

    await handle_payload(c, _payload())

    expected = ReplyTarget(platform="slack", channel_id="C1", thread_ref="1.0")
    assert c.publisher.replies == [(expected, "完成")]


async def test_飞书平台回帖按instance平台分发() -> None:
    """飞书实例回帖同样经 publisher 端口，目标由 instance + task 拼出。"""
    feishu_instance = ChannelInstance(
        id="ch_2", platform="feishu", channel_id="oc_1", workspace_id="t_1", agent_identity="ai_2"
    )
    router = FakeRouter()
    c = _container(_task(), feishu_instance, router)

    await handle_payload(c, _payload())

    expected = ReplyTarget(platform="feishu", channel_id="oc_1", thread_ref="1.0")
    assert c.publisher.replies == [(expected, "完成")]


async def test_decision无文案时不回帖() -> None:
    router = FakeRouter(decision=RoutingDecision(handler="respond", message=""))
    c = _container(_task(), _instance(), router)

    await handle_payload(c, _payload())

    assert c.publisher.replies == []


@pytest.fixture
def fast_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    """把 worker 的两个等待常量压到毫秒级，否则每个循环测试都要真等几秒。

    DEQUEUE_BLOCK_SECONDS 传给 dequeue（FakeTaskQueue 自己也封了 0.01 上限）；
    ERROR_RETRY_SECONDS 是队列报错后的重试间隔，那条路径走真 sleep，不压会
    拖满一秒。
    """
    import app.worker.main as W

    monkeypatch.setattr(W, "DEQUEUE_BLOCK_SECONDS", 0.01)
    monkeypatch.setattr(W, "ERROR_RETRY_SECONDS", 0.01)


async def _run_briefly(container: object, seconds: float = 0.05) -> None:
    """起消费循环、跑一小会儿、置停止位并等它干净退出。"""
    stop = asyncio.Event()
    runner = asyncio.create_task(run_worker(container, stop))
    await asyncio.sleep(seconds)
    stop.set()
    await asyncio.wait_for(runner, timeout=1.0)


async def test_消费循环_取空队列后可被stop打断(fast_worker: None) -> None:
    c = _container(_task(), _instance(), FakeRouter())
    await _run_briefly(c)


async def test_消费循环_处理队列中的载荷(fast_worker: None) -> None:
    router = FakeRouter()
    c = _container(_task(), _instance(), router)
    await c.queue.enqueue(_payload())

    await _run_briefly(c)

    assert len(router.calls) == 1


async def test_消费循环_出队必须带阻塞超时(fast_worker: None) -> None:
    """不带超时就退化成忙循环打满 Redis，这是改用 BLPOP 的全部意义。"""
    import app.worker.main as W

    c = _container(_task(), _instance(), FakeRouter())
    await _run_briefly(c)

    assert c.queue.dequeue_timeouts, "未调用过 dequeue"
    assert all(t == W.DEQUEUE_BLOCK_SECONDS for t in c.queue.dequeue_timeouts)


async def test_队列不可用_循环不崩溃(fast_worker: None) -> None:
    """Redis 挂掉时 worker 应重试而非退出，否则要靠外部拉起。"""

    class BrokenQueue(FakeTaskQueue):
        def __init__(self) -> None:
            super().__init__()
            self.attempts = 0

        async def dequeue(self, timeout_seconds: float = 0):  # type: ignore[no-untyped-def]
            self.attempts += 1
            raise ConnectionError("Redis 不可达（测试注入）")

    c = _container(_task(), _instance(), FakeRouter())
    c.queue = BrokenQueue()

    await _run_briefly(c)

    assert c.queue.attempts >= 1
