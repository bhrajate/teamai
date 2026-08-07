"""事件去重的 Redis 实现（EventDeduplicator 端口），Redis 不可用时降级到内存。"""

from __future__ import annotations

import time

from teamai.config import settings
from teamai.domain.ports import EventDeduplicator
from teamai.infrastructure.redis_client import RedisClientProvider


class InMemoryEventDeduplicator(EventDeduplicator):
    """无 Redis 时的降级实现（开发/测试用）。

    ⚠️ 进程内有效。多 worker 部署时各进程各记一本，跨进程的重投拦不住 ——
    生产须用 Redis 版。
    """

    def __init__(self, ttl_seconds: int | None = None) -> None:
        self._ttl = ttl_seconds if ttl_seconds is not None else settings.event_dedup_ttl_seconds
        self._seen: dict[str, float] = {}

    async def is_duplicate(self, event_key: str) -> bool:
        now = time.monotonic()
        self._evict_expired(now)
        if event_key in self._seen:
            return True
        self._seen[event_key] = now + self._ttl
        return False

    def _evict_expired(self, now: float) -> None:
        if not self._seen:
            return
        expired = [k for k, deadline in self._seen.items() if deadline <= now]
        for k in expired:
            del self._seen[k]


class RedisEventDeduplicator(EventDeduplicator):
    """Redis 实现。Redis 不可用时回退到内存实现。

    用 `SET key 1 NX EX ttl` 一次往返完成检查与登记：NX 让写入只在键不存在时
    成功，返回值即「是否首次」。不用 EXISTS + SET 两步，那样并发重投会双双通过。

    client 由共享的 RedisClientProvider 提供而非每次新建：本方法每条 Slack
    消息都要走一次，逐次建连会给每条消息凭空加上一次 TCP 握手的延迟。
    """

    def __init__(self, redis: RedisClientProvider | None = None, ttl_seconds: int | None = None) -> None:
        self._redis = redis or RedisClientProvider()
        self._ttl = ttl_seconds if ttl_seconds is not None else settings.event_dedup_ttl_seconds
        self._fallback = InMemoryEventDeduplicator(self._ttl)

    async def is_duplicate(self, event_key: str) -> bool:
        try:
            client = self._redis.client()
            first_time = await client.set(f"dedup:{event_key}", "1", nx=True, ex=self._ttl)
            return not first_time
        except Exception:  # pragma: no cover - 外部服务不可用
            # 降级而非放行：Redis 挂了也不该把重投当新事件处理。
            return await self._fallback.is_duplicate(event_key)


def build_event_deduplicator(redis: RedisClientProvider | None = None) -> EventDeduplicator:
    return RedisEventDeduplicator(redis)
