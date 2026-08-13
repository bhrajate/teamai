"""MessageRouter 测试，重点是同步/异步两条链路的分叉。

长任务链路的契约有三条，都在这里锁住：
1. 判定为长任务时只入队，不在 web 进程里执行 Agent（否则拆进程就白拆了）；
2. 入队后任务留在 PENDING —— RUNNING 由 worker 推进，用于区分「排队中」与
   「执行中」；
3. 队列不可用时降级为同步执行，而不是把 ConnectionError 抛给平台。
"""

from __future__ import annotations

import pytest

from teamai.application.orchestrator import TaskOrchestrator
from teamai.application.router import MessageRouter
from teamai.domain.models import TaskStatus
from teamai.domain.ports import ThreadMessage
from teamai.domain.services import AuditLogWriter
from tests.doubles import (
    FakeChannels,
    FakeConversation,
    FakeDistiller,
    FakeIntentClassifier,
    FakeMemory,
    FakePolicyRepo,
    FakeRuntime,
    FakeTags,
    mention,
)
from tests.fakes import FakeAuditRepository, FakeTaskQueue, FakeTaskRepository


def _build(kind: str, queue: FakeTaskQueue) -> tuple[MessageRouter, FakeRuntime, FakeTaskRepository]:
    router, runtime, task_repo, _, _ = _build_full(kind, queue)
    return router, runtime, task_repo


def _build_full(
    kind: str,
    queue: FakeTaskQueue,
    *,
    conversation: FakeConversation | None = None,
    distiller: FakeDistiller | None = None,
) -> tuple[MessageRouter, FakeRuntime, FakeTaskRepository, FakeDistiller, FakeConversation]:
    """完整装配，返回新增的两个协作者以便断言。

    与 `_build` 分开是为了不改动既有测试的解包形式 —— 长任务分叉那批断言
    与本次改造无关，不该因为多了两个依赖而全部重写。
    """
    task_repo = FakeTaskRepository()
    orchestrator = TaskOrchestrator(task_repo, AuditLogWriter(FakeAuditRepository()), queue)
    runtime = FakeRuntime()
    dist = distiller if distiller is not None else FakeDistiller()
    conv = conversation if conversation is not None else FakeConversation()
    router = MessageRouter(
        orchestrator=orchestrator,
        intent=FakeIntentClassifier(kind),
        tags=FakeTags(),
        memory=FakeMemory(),
        budget=object(),
        runtime=runtime,
        channels=FakeChannels(),
        policy_repo=FakePolicyRepo(),
        conversation=conv,
        distiller=dist,
    )
    return router, runtime, task_repo, dist, conv


# ===== 长任务：只入队，不执行 =====


@pytest.mark.parametrize(
    "kind", ["code_review", "bugfix", "data_analysis", "documentation", "pr_operation"]
)
async def test_长任务入队且不在web进程执行(kind: str) -> None:
    queue = FakeTaskQueue()
    router, runtime, _ = _build(kind, queue)

    decision = await router.route(mention())

    assert len(queue.enqueued) == 1, "长任务必须入队"
    assert runtime.runs == 0, "长任务不该在 web 进程里跑 Agent"
    assert decision.handler == "respond"
    assert "已受理" in decision.message


async def test_长任务入队后状态留在PENDING() -> None:
    """RUNNING 由 worker 推进，以此区分「排队中」与「执行中」。"""
    queue = FakeTaskQueue()
    router, _, task_repo = _build("code_review", queue)

    await router.route(mention())

    task = next(iter(task_repo.items.values()))
    assert task.status is TaskStatus.PENDING


async def test_入队载荷携带原始指令与线程引用() -> None:
    """tasks 表只存 intent 不存原文，prompt 必须由载荷带走，否则 worker 拿不到指令。"""
    queue = FakeTaskQueue()
    router, _, task_repo = _build("code_review", queue)

    await router.route(mention("帮我审查 payment 模块"))

    payload = queue.enqueued[0]
    task = next(iter(task_repo.items.values()))
    assert payload.task_id == task.id
    assert payload.prompt == "帮我审查 payment 模块"
    assert payload.thread_ref == "1700000000.1"
    assert payload.channel_instance_id == "ch_1"
    assert payload.model_level == task.model_level


# ===== 短任务：同步执行，不入队 =====


@pytest.mark.parametrize("kind", ["query", "chat", "ticket", "general_task"])
async def test_短任务同步执行且不入队(kind: str) -> None:
    queue = FakeTaskQueue()
    router, runtime, task_repo = _build(kind, queue)

    decision = await router.route(mention("现在几点"))

    assert queue.enqueued == []
    assert runtime.runs == 1
    assert decision.message == "执行完毕"
    assert next(iter(task_repo.items.values())).status is TaskStatus.DONE


# ===== 降级 =====


async def test_入队失败降级为同步执行() -> None:
    """Redis 挂掉时功能不能整体失效，代价是本次响应变慢。"""
    queue = FakeTaskQueue()
    queue.fail_next = True
    router, runtime, task_repo = _build("code_review", queue)

    decision = await router.route(mention())

    assert queue.enqueued == []
    assert runtime.runs == 1, "入队失败后应就地执行"
    assert decision.message == "执行完毕"
    assert next(iter(task_repo.items.values())).status is TaskStatus.DONE


async def test_入队失败不重复建任务() -> None:
    """降级路径复用已落库的那个任务，不该留下多余的 PENDING 记录。"""
    queue = FakeTaskQueue()
    queue.fail_next = True
    router, _, task_repo = _build("code_review", queue)

    await router.route(mention())

    assert task_repo.create_calls == 1
    assert len(task_repo.items) == 1


# ===== 非 @ 消息 =====


async def test_非mention消息只记上下文不建任务() -> None:
    queue = FakeTaskQueue()
    router, runtime, task_repo = _build("code_review", queue)

    decision = await router.route(mention("随便聊聊", is_mention=False))

    assert decision.handler == "observe"
    assert task_repo.create_calls == 0
    assert queue.enqueued == []
    assert runtime.runs == 0


async def test_非mention消息进蒸馏窗口而不直接写记忆() -> None:
    """回归点：此前每条非 @ 消息都被当成一条「记忆」直接落库，于是
    memory_entries 退化成掐掉 500 字的聊天日志，向量检索信噪比被稀释。"""
    queue = FakeTaskQueue()
    router, _, _, distiller, _ = _build_full("chitchat", queue)

    await router.route(mention("这个服务的超时是 30 秒", is_mention=False))

    assert distiller.observed == [("ch_1", "U1", "这个服务的超时是 30 秒")]


@pytest.mark.parametrize("channel_type", ["im", "mpim", "p2p"])
async def test_私密会话不进记忆(channel_type: str) -> None:
    """PRD §4.2：私密频道与私信内容默认不进入记忆。

    此前 Visibility 枚举建好了却无人判定，单聊内容照样进频道记忆 ——
    承诺写在文档里但没落地。
    """
    queue = FakeTaskQueue()
    router, _, _, distiller, _ = _build_full("chitchat", queue)

    decision = await router.route(
        mention("我的密码是 hunter2", is_mention=False, channel_type=channel_type)
    )

    assert distiller.observed == [], f"{channel_type} 的内容不该进窗口"
    assert decision.handler == "observe"


async def test_斜杠与空白消息不进窗口() -> None:
    """斜杠开头是给别的机器人或平台自身的指令，不是团队对话内容。"""
    queue = FakeTaskQueue()
    router, _, _, distiller, _ = _build_full("chitchat", queue)

    await router.route(mention("/deploy prod", is_mention=False))
    await router.route(mention("   ", is_mention=False))

    assert distiller.observed == []


# ===== 线程历史 =====


async def test_线程历史接进上下文() -> None:
    """回归点：thread_history 字段与压缩逻辑早就写好了，但生产代码从不赋值，
    全仓库只有测试在填 —— 机器人被 @ 时看不见上一句在说什么。"""
    queue = FakeTaskQueue()
    history = [ThreadMessage(author_id="U9", text="部署卡在第三步")]
    router, runtime, _, _, conv = _build_full(
        "chitchat", queue, conversation=FakeConversation(history)
    )

    await router.route(mention())

    assert conv.calls == [("C1", "1700000000.1")], "应按频道与线程锚点拉取"
    bundle = runtime.bundles[0]
    assert [m.text for m in bundle.thread_history] == ["部署卡在第三步"]


# ===== 线程历史缓存的回填 =====


async def test_入向消息与机器人回复都回填() -> None:
    """缓存靠这个保持新鲜：否则 TTL 窗口内的第二轮对话看不见机器人上一句。"""
    queue = FakeTaskQueue()
    router, _, _, _, conv = _build_full("chitchat", queue)

    await router.route(mention("帮我看下"))

    assert conv.noted == [
        (False, "1700000000.1", "帮我看下"),
        (True, "1700000000.1", "执行完毕"),
    ]


async def test_非mention消息也回填() -> None:
    """非 @ 消息同样在线程里，平台拉取时会返回它们 —— 只记 @ 消息会让缓存与
    平台不一致。这与「进不进记忆窗口」是两回事。"""
    queue = FakeTaskQueue()
    router, _, _, _, conv = _build_full("chitchat", queue)

    await router.route(mention("随口一句", is_mention=False))

    assert conv.noted == [(False, "1700000000.1", "随口一句")]


@pytest.mark.parametrize("channel_type", ["im", "mpim", "p2p"])
async def test_私密会话仍回填线程缓存(channel_type: str) -> None:
    """PRD §4.2 管的是「不进记忆」（跨会话留存），不是「不看当前对话」。缓存是
    平台数据的秒级镜像、45 秒后即消失，单聊里机器人本就看得见这些话。"""
    queue = FakeTaskQueue()
    router, _, _, distiller, conv = _build_full("chitchat", queue)

    await router.route(mention("私事", is_mention=False, channel_type=channel_type))

    assert distiller.observed == [], "不该进记忆窗口"
    assert conv.noted == [(False, "1700000000.1", "私事")], "但该进线程缓存"


async def test_受理确认与失败文案都回填() -> None:
    """判据是「这条文案会不会发到平台」：会发的就该在缓存里，否则机器人看到的
    历史会随缓存是否命中而不同。"""
    queue = FakeTaskQueue()
    router, _, _, _, conv = _build_full("code_review", queue)  # 长任务：只入队 + 回受理

    await router.route(mention("审一下"))

    assert conv.noted[1][0] is True
    assert "任务已受理" in conv.noted[1][2]


async def test_observe文案不回填() -> None:
    """`_observe` 的返回值两个平台都不发送（Slack 丢弃，飞书只在 is_mention 时回），
    把它记进缓存会凭空多出平台上不存在的消息。"""
    queue = FakeTaskQueue()
    router, _, _, _, conv = _build_full("chitchat", queue)

    decision = await router.route(mention("/deploy prod", is_mention=False))

    assert decision.message == "已忽略"
    assert conv.noted == [(False, "1700000000.1", "/deploy prod")], "只该有入向那条"


async def test_未装配会话服务时照常执行() -> None:
    """线程历史是增益不是依赖：没配 reader（平台凭据不全）时任务照跑。"""
    queue = FakeTaskQueue()
    task_repo = FakeTaskRepository()
    orchestrator = TaskOrchestrator(task_repo, AuditLogWriter(FakeAuditRepository()), queue)
    runtime = FakeRuntime()
    router = MessageRouter(
        orchestrator=orchestrator,
        intent=FakeIntentClassifier("chitchat"),
        tags=FakeTags(),
        memory=FakeMemory(),
        budget=object(),
        runtime=runtime,
        channels=FakeChannels(),
        policy_repo=FakePolicyRepo(),
    )

    decision = await router.route(mention())

    assert decision.message == "执行完毕"
    assert runtime.bundles[0].thread_history == []
