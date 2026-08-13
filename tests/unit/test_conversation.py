"""ConversationService 与线程读取注册表/缓存测试。

两条核心契约：

1. **拉不到历史不是错误。** 线程历史是增益，没有它仍能作答，所以整条链路
   （平台实现 → 注册表 → 缓存 → 服务）在任何一环失败时都降级为空列表，不向上抛。
   这条约定让 router 不必为「平台限流」写分支。
2. **缓存必须能自更新。** 线程历史不是不可变对象 —— 每来一条消息它就变了。只靠
   TTL 整体重建会让窗口内的第二轮对话看不见机器人上一句，而多轮对话恰恰是唯一会
   连续读同一线程的场景。故已知的新消息经 note 回填，见「自更新」两节。
"""

from __future__ import annotations

from datetime import UTC, datetime

from teamai.application.conversation import ConversationService
from teamai.domain.models import ChannelInstance
from teamai.domain.ports import (
    ThreadHistorySink,
    ThreadLocator,
    ThreadMessage,
    ThreadReader,
)
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


# ===== ConversationService 的回填入口 =====


class StubSink(ThreadHistorySink):
    def __init__(self, *, boom: bool = False) -> None:
        self.noted: list[tuple[ThreadLocator, ThreadMessage]] = []
        self._boom = boom

    async def note(self, locator: ThreadLocator, message: ThreadMessage) -> None:
        if self._boom:
            raise ConnectionError("缓存挂了")
        self.noted.append((locator, message))


async def test_入向消息按用户身份回填() -> None:
    sink = StubSink()
    service = ConversationService(StubReader(), sink=sink)

    await service.note_inbound(INSTANCE, "t1", "U7", "用户说的话")

    (locator, message) = sink.noted[0]
    assert locator == ThreadLocator(platform="slack", channel_id="C1", thread_ref="t1")
    assert (message.author_id, message.text, message.is_self) == ("U7", "用户说的话", False)


async def test_出向回复标为机器人() -> None:
    """否则下一轮的提示词会把机器人自己的输出渲染成某个用户的诉求。"""
    sink = StubSink()
    service = ConversationService(StubReader(), sink=sink)

    await service.note_outbound(INSTANCE, "t1", "机器人的回答")

    message = sink.noted[0][1]
    assert message.is_self is True
    assert message.author_id == INSTANCE.agent_identity


async def test_没有sink时回填是空操作() -> None:
    """无 Redis 的部署里缓存本身不存在，缺它只是退回「每次读都打平台」。"""
    service = ConversationService(StubReader())

    await service.note_inbound(INSTANCE, "t1", "U7", "话")
    await service.note_outbound(INSTANCE, "t1", "回答")


async def test_空锚点与空正文不回填() -> None:
    sink = StubSink()
    service = ConversationService(StubReader(), sink=sink)

    await service.note_inbound(INSTANCE, "", "U7", "有正文但没锚点")
    await service.note_inbound(INSTANCE, "t1", "U7", "")
    await service.note_outbound(INSTANCE, "t1", "")

    assert sink.noted == []


async def test_回填失败不外抛() -> None:
    """回填失败的代价是这条消息在当前 TTL 窗口内缺席，不该连带影响建任务。"""
    service = ConversationService(StubReader(), sink=StubSink(boom=True))

    await service.note_inbound(INSTANCE, "t1", "U7", "话")


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


class FakePipeline:
    """攒命令、execute 时按序执行。真 pipeline 的原子性不在单测覆盖范围内，
    这里只保证「命令都执行了、顺序没错」。"""

    def __init__(self, redis: FakeRedis) -> None:
        self._redis = redis
        self._queued: list[tuple[str, tuple]] = []

    def delete(self, key: str) -> None:
        self._queued.append(("delete", (key,)))

    def rpush(self, key: str, *values: str) -> None:
        self._queued.append(("rpush", (key, *values)))

    def rpushx(self, key: str, *values: str) -> None:
        self._queued.append(("rpushx", (key, *values)))

    def ltrim(self, key: str, start: int, end: int) -> None:
        self._queued.append(("ltrim", (key, start, end)))

    def expire(self, key: str, ttl: int) -> None:
        self._queued.append(("expire", (key, ttl)))

    async def execute(self) -> list:
        out = []
        for name, args in self._queued:
            out.append(getattr(self._redis, f"_sync_{name}")(*args))
        self._queued.clear()
        return out


class FakeRedis:
    """LIST 语义的内存替身，附带故障开关。

    只实现被用到的命令。RPUSHX 的「键不存在则不建键」是被测语义的核心，
    故与真 Redis 严格一致。TTL 只记不判过期：过期行为由 Redis 保证，测的是
    「写快照时设了 TTL、追加时没重设」。
    """

    def __init__(self, *, boom: bool = False) -> None:
        self.lists: dict[str, list[str]] = {}
        self.expires: list[tuple[str, int]] = []
        self._boom = boom

    def client(self):
        if self._boom:
            raise ConnectionError("Redis 挂了")
        return self

    def pipeline(self) -> FakePipeline:
        return FakePipeline(self)

    async def lrange(self, key: str, start: int, end: int) -> list[str]:
        items = self.lists.get(key, [])
        return items[start:] if end == -1 else items[start : end + 1]

    # pipeline 内的同步执行体
    def _sync_delete(self, key: str) -> int:
        return 1 if self.lists.pop(key, None) is not None else 0

    def _sync_rpush(self, key: str, *values: str) -> int:
        self.lists.setdefault(key, []).extend(values)
        return len(self.lists[key])

    def _sync_rpushx(self, key: str, *values: str) -> int:
        """键不存在时不建键，返回 0 —— 与真 RPUSHX 一致。"""
        if key not in self.lists:
            return 0
        self.lists[key].extend(values)
        return len(self.lists[key])

    def _sync_ltrim(self, key: str, start: int, end: int) -> bool:
        if key not in self.lists:
            return True
        items = self.lists[key]
        self.lists[key] = items[start:] if end == -1 else items[start : end + 1]
        return True

    def _sync_expire(self, key: str, ttl: int) -> bool:
        self.expires.append((key, ttl))
        return True


def test_编解码保真() -> None:
    for original in (
        ThreadMessage(author_id="U1", text="人说的", ts=datetime(2026, 1, 1, tzinfo=UTC)),
        ThreadMessage(author_id="B1", text="机器人说的", is_self=True),
    ):
        assert _decode(_encode(original)) == original


LOCATOR = ThreadLocator(platform="slack", channel_id="C1", thread_ref="t1")


async def test_二次读取命中缓存不打平台() -> None:
    inner = StubReader([ThreadMessage(author_id="U1", text="某句")])
    cached = CachedThreadReader(inner, FakeRedis(), ttl_seconds=45)

    first = await cached.fetch_thread(LOCATOR, 10)
    second = await cached.fetch_thread(LOCATOR, 10)

    assert first == second
    assert len(inner.calls) == 1, "第二次该命中缓存"


async def test_按缓存容量拉取而非按本次limit() -> None:
    """多拉的部分留给后续更大的请求复用 —— 反正是同一次 API 调用，
    而平台配额是按调用次数算的。"""
    inner = StubReader([ThreadMessage(author_id="U1", text=f"第 {i} 句") for i in range(5)])
    cached = CachedThreadReader(inner, FakeRedis(), cache_limit=30)

    await cached.fetch_thread(LOCATOR, 3)

    assert inner.calls[0][1] == 30


async def test_小limit从缓存切尾而不重打平台() -> None:
    """键里不再含 limit，一份缓存服务全部不超过容量的请求。"""
    inner = StubReader([ThreadMessage(author_id="U1", text=f"第 {i} 句") for i in range(5)])
    cached = CachedThreadReader(inner, FakeRedis(), cache_limit=30)

    await cached.fetch_thread(LOCATOR, 30)
    few = await cached.fetch_thread(LOCATOR, 2)

    assert [m.text for m in few] == ["第 3 句", "第 4 句"], "该切最近 2 条"
    assert len(inner.calls) == 1


async def test_超出缓存容量的请求绕过缓存() -> None:
    """否则会把「缓存只存了 30 条」误当成「线程只有 30 条」返回给要 50 条的调用方。"""
    inner = StubReader([ThreadMessage(author_id="U1", text="某句")])
    redis = FakeRedis()
    cached = CachedThreadReader(inner, redis, cache_limit=30)

    await cached.fetch_thread(LOCATOR, 50)

    assert inner.calls[0][1] == 50
    assert redis.lists == {}, "绕过缓存的结果不该写进缓存（它按容量截过尾）"


async def test_空结果不入缓存() -> None:
    """拉取失败也返回空列表，缓存它会把一次偶发失败固化整个 TTL 窗口。"""
    inner = StubReader([])
    redis = FakeRedis()
    cached = CachedThreadReader(inner, redis)

    await cached.fetch_thread(LOCATOR, 10)
    await cached.fetch_thread(LOCATOR, 10)

    assert redis.lists == {}
    assert len(inner.calls) == 2, "空结果每次都该重试"


async def test_Redis不可用时穿透到平台() -> None:
    """缓存的作用是省配额，不是正确性依赖。"""
    inner = StubReader([ThreadMessage(author_id="U1", text="某句")])
    cached = CachedThreadReader(inner, FakeRedis(boom=True))

    messages = await cached.fetch_thread(LOCATOR, 10)

    assert [m.text for m in messages] == ["某句"]


async def test_缓存TTL按配置写入() -> None:
    redis = FakeRedis()
    cached = CachedThreadReader(
        StubReader([ThreadMessage(author_id="U1", text="x")]), redis, ttl_seconds=45
    )

    await cached.fetch_thread(LOCATOR, 10)

    assert redis.expires == [("thread:slack:C1:t1", 45)]


# ===== 自更新：note 回填 =====


async def test_机器人自己的回复在TTL窗口内可见() -> None:
    """本次改造要修的就是这个：原先缓存只能靠 TTL 整体重建，于是窗口内的第二轮
    对话看不到机器人上一句，「第二个方案细化下」就无从理解。"""
    inner = StubReader([ThreadMessage(author_id="U1", text="列几个方案")])
    cached = CachedThreadReader(inner, FakeRedis())

    await cached.fetch_thread(LOCATOR, 10)  # 第一轮：写入快照
    await cached.note(LOCATOR, ThreadMessage(author_id="B1", text="方案一二三", is_self=True))
    await cached.note(LOCATOR, ThreadMessage(author_id="U1", text="第二个细化下"))

    second = await cached.fetch_thread(LOCATOR, 10)

    assert [m.text for m in second] == ["列几个方案", "方案一二三", "第二个细化下"]
    assert len(inner.calls) == 1, "全程只该打一次平台"
    assert [m.is_self for m in second] == [False, True, False]


async def test_无缓存时追加不建键() -> None:
    """RPUSHX 而非 RPUSH。否则一条追加就凭空造出「只有一条消息的线程历史」，
    下次读取命中它、把真实历史整个挡住 —— 比缓存陈旧严重得多。"""
    inner = StubReader([ThreadMessage(author_id="U1", text=f"平台第 {i} 句") for i in range(3)])
    redis = FakeRedis()
    cached = CachedThreadReader(inner, redis)

    await cached.note(LOCATOR, ThreadMessage(author_id="U1", text="孤零零一句"))

    assert redis.lists == {}, "不该建键"
    messages = await cached.fetch_thread(LOCATOR, 10)
    assert [m.text for m in messages] == ["平台第 0 句", "平台第 1 句", "平台第 2 句"]


async def test_追加不续期() -> None:
    """TTL 到点整体重建是这套机制的纠错手段：追加过程中丢的、重的、乱序的，
    都在下个窗口被平台数据抹平。每次追加都续期会让一次错误追加永久驻留。"""
    redis = FakeRedis()
    cached = CachedThreadReader(
        StubReader([ThreadMessage(author_id="U1", text="x")]), redis, ttl_seconds=45
    )
    await cached.fetch_thread(LOCATOR, 10)

    await cached.note(LOCATOR, ThreadMessage(author_id="U1", text="又一句"))

    assert redis.expires == [("thread:slack:C1:t1", 45)], "只该有快照写入时那一次"


async def test_追加按容量截尾() -> None:
    """持续有人说话的线程会一直追加，不截会无界增长 —— TTL 只是上限不是约束。"""
    inner = StubReader([ThreadMessage(author_id="U1", text="最早的")])
    redis = FakeRedis()
    cached = CachedThreadReader(inner, redis, cache_limit=3)
    await cached.fetch_thread(LOCATOR, 3)

    for i in range(5):
        await cached.note(LOCATOR, ThreadMessage(author_id="U1", text=f"新 {i}"))

    messages = await cached.fetch_thread(LOCATOR, 3)
    assert [m.text for m in messages] == ["新 2", "新 3", "新 4"]
    assert len(redis.lists["thread:slack:C1:t1"]) == 3


async def test_追加空正文与空锚点都跳过() -> None:
    redis = FakeRedis()
    cached = CachedThreadReader(StubReader([ThreadMessage(author_id="U1", text="x")]), redis)
    await cached.fetch_thread(LOCATOR, 10)
    before = list(redis.lists["thread:slack:C1:t1"])

    await cached.note(LOCATOR, ThreadMessage(author_id="U1", text=""))
    await cached.note(
        ThreadLocator(platform="slack", channel_id="C1", thread_ref=""),
        ThreadMessage(author_id="U1", text="有正文但没锚点"),
    )

    assert redis.lists["thread:slack:C1:t1"] == before


async def test_Redis不可用时追加静默失败() -> None:
    """回填是缓存的自我维护，失败的代价是这条消息在当前窗口缺席。"""
    cached = CachedThreadReader(StubReader(), FakeRedis(boom=True))

    await cached.note(LOCATOR, ThreadMessage(author_id="U1", text="某句"))


# ===== 渲染 =====


def test_机器人发言标为AI() -> None:
    """混作一堆无署名文本时，模型容易把自己上一轮输出当成用户诉求。"""
    assert ThreadMessage(author_id="U1", text="问题").render() == "U1: 问题"
    assert ThreadMessage(author_id="B1", text="回答", is_self=True).render() == "AI: 回答"


def test_发送者缺失时标为unknown() -> None:
    assert ThreadMessage(author_id="", text="系统消息").render() == "unknown: 系统消息"
