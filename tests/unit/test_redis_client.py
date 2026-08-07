"""共享 Redis client 的复用、保活与关闭。

原先 queue 与 dedup 每次调用都 from_url + aclose，每次都摊上一次 TCP 握手加
断开（本地回环实测 6.5ms vs 复用 0.75ms）。改为复用后，「只建一次」「退出时
关掉」「空闲连接保活」这三点成了对外承诺，这里固化住。
"""

from __future__ import annotations

import pytest

from teamai.infrastructure.redis_client import RedisClientProvider


class _FakeClient:
    def __init__(self) -> None:
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


@pytest.fixture
def patched(monkeypatch: pytest.MonkeyPatch) -> dict:
    """替掉 from_url，记录调用次数与参数。"""
    import redis.asyncio as aioredis

    state: dict = {"count": 0, "kwargs": {}, "clients": []}

    def _factory(*a: object, **kw: object) -> _FakeClient:
        state["count"] += 1
        state["kwargs"] = kw
        client = _FakeClient()
        state["clients"].append(client)
        return client

    monkeypatch.setattr(aioredis, "from_url", _factory)
    return state


class TestReuse:
    def test_首次取用才创建(self, patched: dict) -> None:
        provider = RedisClientProvider("redis://x:6379/0")
        assert patched["count"] == 0, "构造 provider 不该就去建 client"
        provider.client()
        assert patched["count"] == 1

    def test_反复取用只创建一次(self, patched: dict) -> None:
        provider = RedisClientProvider("redis://x:6379/0")
        first = provider.client()
        for _ in range(20):
            assert provider.client() is first
        assert patched["count"] == 1

    def test_两个_provider_互不共享(self, patched: dict) -> None:
        """provider 是实例级缓存而非模块级单例。

        模块级单例会把连接绑死在创建它的事件循环上，跨循环使用直接抛
        Event loop is closed；本项目 pytest-asyncio 每个测试一个新循环，
        那样会搞坏整个测试套件。
        """
        a = RedisClientProvider("redis://x:6379/0")
        b = RedisClientProvider("redis://x:6379/0")
        assert a.client() is not b.client()
        assert patched["count"] == 2


class TestKeepAlive:
    def test_开启健康检查与_keepalive(self, patched: dict) -> None:
        """连接改为长期存活后新增的需求。

        空闲连接可能被防火墙/LB 静默掐断，TCP 层要到下次写入才发现。
        health_check_interval 让 redis-py 复用空闲连接前先 PING。
        """
        RedisClientProvider("redis://x:6379/0").client()
        kwargs = patched["kwargs"]
        assert kwargs.get("health_check_interval") == 30, "缺健康检查则掐断的空闲连接会把错误抛给调用方"
        assert kwargs.get("socket_keepalive") is True


class TestClose:
    async def test_关闭后置空_可再次创建(self, patched: dict) -> None:
        provider = RedisClientProvider("redis://x:6379/0")
        client = provider.client()
        await provider.aclose()
        assert client.closed is True
        provider.client()
        assert patched["count"] == 2, "关闭后应能重新创建"

    async def test_未创建时关闭不抛(self, patched: dict) -> None:
        await RedisClientProvider("redis://x:6379/0").aclose()
        assert patched["count"] == 0

    async def test_重复关闭不抛(self, patched: dict) -> None:
        provider = RedisClientProvider("redis://x:6379/0")
        provider.client()
        await provider.aclose()
        await provider.aclose()


class TestContainerTeardown:
    async def test_container_aclose_关掉共享连接池(self, patched: dict) -> None:
        """长期存活的连接必须由进程入口显式收尾，否则退出时留下未关闭 socket。"""
        from teamai.container import build_container

        container = build_container()
        client = container.redis.client()
        assert client.closed is False
        await container.aclose()
        assert client.closed is True

    async def test_queue_与_dedup_共用同一个_provider(self, patched: dict) -> None:
        """各自新建 provider 的话会有两个连接池，空闲连接各占一份。"""
        from teamai.container import build_container

        container = build_container()
        # 两个实现都持有容器建的那一个 provider
        assert container.queue._redis is container.redis  # type: ignore[attr-defined]
        assert container.dedup._redis is container.redis  # type: ignore[attr-defined]

        container.redis.client()
        assert patched["count"] == 1, f"应只有一个连接池，实际 {patched['count']} 个"
        await container.aclose()
