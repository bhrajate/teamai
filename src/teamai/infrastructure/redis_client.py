"""进程内共享的 Redis client。

原先 queue 与 dedup 各自「每次 from_url + 命令 + aclose」：from_url 本身不建连，
但首条命令会建、aclose 会断，于是每次调用都摊上一次 TCP 握手加断开。本地回环
实测 6.5ms/次 vs 复用 0.75ms/次（快 8.7 倍），Redis 跨网段时差距更大。而
dedup 是每条 Slack 消息都要走一次，queue 则被 worker 每秒轮询一次。

不做模块级单例：redis-py 的连接绑定创建它的事件循环，模块级缓存跨循环使用会
抛 `Event loop is closed`。本项目 pytest-asyncio 是 auto 模式、每个测试一个新
循环，模块级缓存会直接搞坏测试。故缓存挂在实例上，由已是进程级单例的
`get_container()` 持有一个 provider。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from teamai.config import settings

if TYPE_CHECKING:
    from redis.asyncio import Redis


class RedisClientProvider:
    """懒建并复用一个 Redis client（即一个连接池）。

    queue 与 dedup 共用同一个 provider，因此全进程只有一个连接池 —— 各自持有
    的话空闲连接会各占一份。
    """

    def __init__(self, redis_url: str | None = None) -> None:
        self._redis_url = redis_url or settings.redis_url
        self._client: Redis | None = None

    def client(self) -> Redis:
        """取共享 client。首次调用时创建，此时尚不建立连接。"""
        if self._client is None:
            import redis.asyncio as aioredis

            self._client = aioredis.from_url(
                self._redis_url,
                # 连接改为长期存活后新出现的隐患：空闲连接可能被防火墙/LB 静默
                # 掐断，而 TCP 层要到下次写入才发现。健康检查让 redis-py 在复用
                # 空闲超过 30s 的连接前先 PING 一次，掐断的连接就地重建，
                # 不会把错误抛给调用方。每次新建连接的旧写法碰不到这个问题。
                health_check_interval=30,
                socket_keepalive=True,
            )
        return self._client

    async def aclose(self) -> None:
        """关闭连接池。由 Container.aclose() 在进程退出时调用。

        长期存活的连接必须显式关掉：靠 GC 回收会留下未关闭的 socket，
        并在退出时打出 unclosed connection 警告。
        """
        if self._client is not None:
            await self._client.aclose()
            self._client = None
