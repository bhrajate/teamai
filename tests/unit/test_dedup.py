"""Slack 事件去重：去重键选取、内存实现语义、处理器拦截。

Slack 会重投事件，不去重则一条 @提及被重复建任务、重复调 LLM、重复回复。
这里覆盖三层：键怎么取、记账本怎么记、处理器是否真的拦住。
"""

from __future__ import annotations

import asyncio

import pytest

from teamai.adapters.slack import dedup_key
from teamai.domain.ports import EventDeduplicator
from teamai.infrastructure.dedup import InMemoryEventDeduplicator


class TestDedupKey:
    def test_优先取信封的_event_id(self) -> None:
        body = {"event_id": "Ev123", "event": {"channel": "C1", "ts": "1.0"}}
        assert dedup_key(body) == "Ev123"

    def test_无_event_id_时退回_channel_ts_subtype(self) -> None:
        body = {"event": {"channel": "C1", "ts": "1.0", "subtype": "edited"}}
        assert dedup_key(body) == "C1:1.0:edited"

    def test_信封为空也不抛(self) -> None:
        assert dedup_key({}) == "::"

    def test_event_为_None_不抛(self) -> None:
        """Slack 偶发送来 event: null，get 的默认值兜不住 None。"""
        assert dedup_key({"event": None}) == "::"

    def test_同一事件重投得到同一个键(self) -> None:
        first = {"event_id": "Ev9", "event": {"channel": "C1", "ts": "1.0"}}
        retry = {"event_id": "Ev9", "event": {"channel": "C1", "ts": "1.0"}}
        assert dedup_key(first) == dedup_key(retry)


class TestInMemoryDeduplicator:
    async def test_首次不算重复_第二次算(self) -> None:
        dedup = InMemoryEventDeduplicator()
        assert await dedup.is_duplicate("Ev1") is False
        assert await dedup.is_duplicate("Ev1") is True

    async def test_不同键互不影响(self) -> None:
        dedup = InMemoryEventDeduplicator()
        assert await dedup.is_duplicate("Ev1") is False
        assert await dedup.is_duplicate("Ev2") is False

    async def test_到期后重新放行(self) -> None:
        dedup = InMemoryEventDeduplicator(ttl_seconds=0)
        assert await dedup.is_duplicate("Ev1") is False
        assert await dedup.is_duplicate("Ev1") is False, "TTL 为 0 时记录应立即过期"

    async def test_过期记录会被淘汰(self) -> None:
        dedup = InMemoryEventDeduplicator(ttl_seconds=0)
        for i in range(50):
            await dedup.is_duplicate(f"Ev{i}")
        assert len(dedup._seen) <= 1, "过期记录未淘汰，长跑会漏内存"

    async def test_并发下只放行一次(self) -> None:
        """重投常与首次请求并发到达，检查与登记必须原子。"""
        dedup = InMemoryEventDeduplicator()
        results = await asyncio.gather(*(dedup.is_duplicate("Ev1") for _ in range(20)))
        assert results.count(False) == 1, f"应只有一次放行，实际 {results.count(False)} 次"

    def test_是端口的实现(self) -> None:
        assert issubclass(InMemoryEventDeduplicator, EventDeduplicator)


class _FakeRedisClient:
    """记录 set 调用参数的假 client。"""

    def __init__(self, set_returns: object) -> None:
        self.set_returns = set_returns
        self.calls: list[dict] = []
        self.closed = False

    async def set(self, key: str, value: str, **kwargs: object) -> object:
        self.calls.append({"key": key, "value": value, **kwargs})
        return self.set_returns

    async def aclose(self) -> None:
        self.closed = True


def _patch_redis(monkeypatch: pytest.MonkeyPatch, client: object) -> None:
    """替掉 redis.asyncio.from_url，避免真连 Redis。"""
    import redis.asyncio as aioredis

    monkeypatch.setattr(aioredis, "from_url", lambda *a, **kw: client)


class TestRedisDeduplicator:
    """生产路径：验证原子性依赖的 SET NX EX 调用形状与降级行为。"""

    async def test_用_set_nx_ex_一次往返完成检查与登记(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from teamai.infrastructure.dedup import RedisEventDeduplicator

        client = _FakeRedisClient(set_returns=True)  # True = 键原先不存在
        _patch_redis(monkeypatch, client)

        dedup = RedisEventDeduplicator(ttl_seconds=1800)
        assert await dedup.is_duplicate("Ev1") is False

        assert len(client.calls) == 1, "应只有一次往返，分成 EXISTS + SET 两步会破坏原子性"
        call = client.calls[0]
        assert call["key"] == "dedup:Ev1"
        assert call["nx"] is True, "缺 NX 则并发重投会双双通过"
        assert call["ex"] == 1800, "缺 EX 则记录永不过期"

    async def test_键已存在判为重复(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from teamai.infrastructure.dedup import RedisEventDeduplicator

        # redis-py 在 NX 未写入时返回 None
        _patch_redis(monkeypatch, _FakeRedisClient(set_returns=None))
        dedup = RedisEventDeduplicator()
        assert await dedup.is_duplicate("Ev1") is True

    async def test_用完即关连接(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from teamai.infrastructure.dedup import RedisEventDeduplicator

        client = _FakeRedisClient(set_returns=True)
        _patch_redis(monkeypatch, client)
        await RedisEventDeduplicator().is_duplicate("Ev1")
        assert client.closed is True

    async def test_redis_不可用时降级而非放行(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Redis 挂了也不该把重投当新事件处理。"""
        import redis.asyncio as aioredis

        from teamai.infrastructure.dedup import RedisEventDeduplicator

        def _boom(*a: object, **kw: object) -> object:
            raise ConnectionError("redis down")

        monkeypatch.setattr(aioredis, "from_url", _boom)

        dedup = RedisEventDeduplicator()
        assert await dedup.is_duplicate("Ev1") is False
        assert await dedup.is_duplicate("Ev1") is True, "降级后仍应拦住重投"


class _FakeLogger:
    def __init__(self) -> None:
        self.infos: list[str] = []

    def info(self, msg: str) -> None:
        self.infos.append(msg)

    def error(self, msg: str) -> None:  # pragma: no cover - 本测试不触发
        self.infos.append(msg)


class TestHandlerDedup:
    """直接验证「重投不落到 router」。

    不构造 AsyncApp（需要真实 token），而是复刻处理器的守卫逻辑：
    dedup_key -> is_duplicate -> 命中即 return。
    """

    @pytest.fixture
    def dedup(self) -> InMemoryEventDeduplicator:
        return InMemoryEventDeduplicator()

    async def test_重投事件不进入下游(self, dedup: InMemoryEventDeduplicator) -> None:
        handled: list[str] = []

        async def handle(body: dict) -> None:
            if await dedup.is_duplicate(dedup_key(body)):
                return
            handled.append(dedup_key(body))

        body = {"event_id": "Ev777", "event": {"channel": "C1", "ts": "1.0"}}
        await handle(body)
        await handle(body)  # 重投
        await handle(body)  # 再重投
        assert handled == ["Ev777"], "同一事件应只被处理一次"

    async def test_不同事件都放行(self, dedup: InMemoryEventDeduplicator) -> None:
        handled: list[str] = []

        async def handle(body: dict) -> None:
            if await dedup.is_duplicate(dedup_key(body)):
                return
            handled.append(dedup_key(body))

        await handle({"event_id": "EvA", "event": {}})
        await handle({"event_id": "EvB", "event": {}})
        assert handled == ["EvA", "EvB"]
