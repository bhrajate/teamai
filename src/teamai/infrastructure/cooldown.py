"""AmbientCooldown 的 Redis 实现（`SET NX EX`），内存兜底。

键空间用 `ambient:` 前缀与 dedup 的 `dedup:` 隔开：两者 TTL 语义不同，
混在一个前缀下会让运维分不清哪条键该多久过期。
"""

from __future__ import annotations

import time

from teamai.domain.ports import AmbientCooldown
from teamai.infrastructure.redis_client import RedisClientProvider


class InMemoryAmbientCooldown(AmbientCooldown):
    """单进程兜底。多副本下各自计冷却，可能重复提醒 —— 仅用于无 Redis 的场景。"""

    def __init__(self) -> None:
        self._until: dict[str, float] = {}

    async def is_cooling(self, key: str, ttl_seconds: int) -> bool:
        now = time.monotonic()
        # 顺手清掉过期项，否则长期运行下 dict 只增不减
        self._until = {k: v for k, v in self._until.items() if v > now}
        if self._until.get(key, 0.0) > now:
            return True
        self._until[key] = now + ttl_seconds
        return False


class RedisAmbientCooldown(AmbientCooldown):
    def __init__(self, redis: RedisClientProvider | None = None) -> None:
        self._redis = redis or RedisClientProvider()
        self._fallback = InMemoryAmbientCooldown()

    async def is_cooling(self, key: str, ttl_seconds: int) -> bool:
        try:
            client = self._redis.client()
            first_time = await client.set(f"ambient:{key}", "1", nx=True, ex=ttl_seconds)
            return not first_time
        except Exception:  # pragma: no cover - 外部服务不可用
            # 降级而非放行：Redis 挂了也不该把冷却期内的提醒当成该发。
            # 与 RedisEventDeduplicator 的取舍一致 —— 宁可少打扰，不可重复打扰。
            return await self._fallback.is_cooling(key, ttl_seconds)


def build_ambient_cooldown(redis: RedisClientProvider | None = None) -> AmbientCooldown:
    return RedisAmbientCooldown(redis)
