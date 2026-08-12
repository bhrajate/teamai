"""ConversationService 与线程读取注册表/缓存测试。

核心契约：**拉不到历史不是错误**。线程历史是增益，没有它仍能作答，所以整条
链路（平台实现 → 注册表 → 缓存 → 服务）在任何一环失败时都降级为空列表，
不向上抛。这条约定让 router 不必为「平台限流」写分支。
"""

from __future__ import annotations

from datetime import UTC, datetime

from teamai.application.conversation import ConversationService
from teamai.domain.models import ChannelInstance
from teamai.domain.ports import ThreadLocator, ThreadMessage, ThreadReader
from teamai.infrastructure.messaging.reader_registry import (
    CachedThreadReader,
    ThreadReaderRegistry,
    _decode,
    _encode,
)

INSTANCE = ChannelInstance(
    id="ch_1", platform="slack", channel_id="C1", workspace_id="T1", agent_identity="ai_1"
)


class StubReader(ThreadReader):
    def __init__(self, messages: list[ThreadMessage] | None = None, *, boom: bool = False) -> None:
        self._messages = messages or []
        self._boom = boom
        self.calls: list[tuple[ThreadLocator, int]] = []

    async def fetch_thread(self, locator: ThreadLocator, limit: int) -> list[ThreadMessage]:
        self.calls.append((locator, limit))
        if self._boom:
            raise ConnectionError("平台挂了")
        return list(self._messages)


# ===== ConversationService =====


async def test_按实例与线程拼出定位() -> None:
    reader = StubReader([ThreadMessage(author_id="U9", text="上一句")])
    service = ConversationService(reader, default_limit=30)

    messages = await service.thread_history(INSTANCE, "1700000000.1")

    (locator, limit) = reader.calls[0]
    assert locator == ThreadLocator(platform="slack", channel_id="C1", thread_ref="1700000000.1")
    assert limit == 30
    assert [m.text for m in messages] == ["上一句"]


async def test_显式limit覆盖默认值() -> None:
    reader = StubReader()
    service = ConversationService(reader, default_limit=30)

    await service.thread_history(INSTANCE, "t1", limit=5)

    assert reader.calls[0][1] == 5


async def test_线程锚点为空时不打平台() -> None:
    """没有锚点意味着无从下手（例如系统触发的任务），不该白发一次请求。"""
    reader = StubReader()
    service = ConversationService(reader)

    assert await service.thread_history(INSTANCE, "") == []
    assert reader.calls == []


async def test_实现违约抛异常时降级为空() -> None:
    """端口契约要求实现自己兜住平台异常；服务再兜一层，代价应是「这次没历史」。"""
    service = ConversationService(StubReader(boom=True))

    assert await service.thread_history(INSTANCE, "t1") == []


# ===== 注册表 =====


async def test_按平台分发() -> None:
    slack = StubReader([ThreadMessage(author_id="U1", text="slack 的")])
    feishu = StubReader([ThreadMessage(author_id="U2", text="飞书的")])
    registry = ThreadReaderRegistry({"slack": slack, "feishu": feishu})

    messages = await registry.fetch_thread(
        ThreadLocator(platform="feishu", channel_id="oc_1", thread_ref="om_1"), 10
    )

    assert [m.text for m in messages] == ["飞书的"]
    assert slack.calls == []


async def test_未注册平台返回空而不抛() -> None:
    """平台凭据不全时就是这个情形 —— 只该丢历史，不该影响任务。"""
    registry = ThreadReaderRegistry()

    result = await registry.fetch_thread(
        ThreadLocator(platform="slack", channel_id="C1", thread_ref="t1"), 10
    )

    assert result == []


# ===== 缓存 =====


class FakeRedis:
    """只实现 get/set 的内存替身，附带故障开关。"""

    def __init__(self, *, boom: bool = False) -> None:
        self.store: dict[str, str] = {}
        self.sets: list[tuple[str, int]] = []
        self._boom = boom

    def client(self):
        if self._boom:
            raise ConnectionError("Redis 挂了")
        return self

    async def get(self, key: str):
        return self.store.get(key)

    async def set(self, key: str, value: str, ex: int | None = None):
        self.store[key] = value
        self.sets.append((key, ex or 0))


def test_编解码保真() -> None:
    original = [
        ThreadMessage(author_id="U1", text="人说的", ts=datetime(2026, 1, 1, tzinfo=UTC)),
        ThreadMessage(author_id="B1", text="机器人说的", is_bot=True),
    ]

    restored = _decode(_encode(original))

    assert restored == original


async def test_二次读取命中缓存不打平台() -> None:
    inner = StubReader([ThreadMessage(author_id="U1", text="某句")])
    cached = CachedThreadReader(inner, FakeRedis(), ttl_seconds=45)
    locator = ThreadLocator(platform="slack", channel_id="C1", thread_ref="t1")

    first = await cached.fetch_thread(locator, 10)
    second = await cached.fetch_thread(locator, 10)

    assert first == second
    assert len(inner.calls) == 1, "第二次该命中缓存"


async def test_不同limit不共用缓存() -> None:
    """要 30 条的调用不该拿到只有 10 条的缓存。"""
    inner = StubReader([ThreadMessage(author_id="U1", text="某句")])
    cached = CachedThreadReader(inner, FakeRedis())
    locator = ThreadLocator(platform="slack", channel_id="C1", thread_ref="t1")

    await cached.fetch_thread(locator, 10)
    await cached.fetch_thread(locator, 30)

    assert len(inner.calls) == 2


async def test_空结果不入缓存() -> None:
    """拉取失败也返回空列表，缓存它会把一次偶发失败固化整个 TTL 窗口。"""
    inner = StubReader([])
    redis = FakeRedis()
    cached = CachedThreadReader(inner, redis)
    locator = ThreadLocator(platform="slack", channel_id="C1", thread_ref="t1")

    await cached.fetch_thread(locator, 10)
    await cached.fetch_thread(locator, 10)

    assert redis.sets == []
    assert len(inner.calls) == 2, "空结果每次都该重试"


async def test_Redis不可用时穿透到平台() -> None:
    """缓存的作用是省配额，不是正确性依赖。"""
    inner = StubReader([ThreadMessage(author_id="U1", text="某句")])
    cached = CachedThreadReader(inner, FakeRedis(boom=True))

    messages = await cached.fetch_thread(
        ThreadLocator(platform="slack", channel_id="C1", thread_ref="t1"), 10
    )

    assert [m.text for m in messages] == ["某句"]


async def test_缓存TTL按配置写入() -> None:
    redis = FakeRedis()
    cached = CachedThreadReader(
        StubReader([ThreadMessage(author_id="U1", text="x")]), redis, ttl_seconds=45
    )

    await cached.fetch_thread(
        ThreadLocator(platform="slack", channel_id="C1", thread_ref="t1"), 10
    )

    assert redis.sets[0][1] == 45


# ===== 渲染 =====


def test_机器人发言标为AI() -> None:
    """混作一堆无署名文本时，模型容易把自己上一轮输出当成用户诉求。"""
    assert ThreadMessage(author_id="U1", text="问题").render() == "U1: 问题"
    assert ThreadMessage(author_id="B1", text="回答", is_bot=True).render() == "AI: 回答"


def test_发送者缺失时标为unknown() -> None:
    assert ThreadMessage(author_id="", text="系统消息").render() == "unknown: 系统消息"
