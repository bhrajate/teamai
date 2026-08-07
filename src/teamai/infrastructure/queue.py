"""任务队列的 Redis 实现（TaskQueue 端口）。"""

from __future__ import annotations

import json
from typing import Any

from teamai.config import settings
from teamai.domain.ports import QueuePayload, TaskQueue


class RedisTaskQueue(TaskQueue):
    def __init__(self, queue_name: str | None = None, redis_url: str | None = None) -> None:
        self._queue_name = queue_name or settings.arq_queue_name
        self._redis_url = redis_url or settings.redis_url

    async def enqueue(self, payload: QueuePayload) -> None:
        try:
            import redis.asyncio as aioredis

            client = aioredis.from_url(self._redis_url)
            await client.rpush(self._queue_name, json.dumps(payload.__dict__))
            await client.aclose()
        except Exception as exc:  # pragma: no cover - 依赖外部服务
            raise ConnectionError(f"入队失败: {exc}") from exc

    async def dequeue(self) -> QueuePayload | None:
        try:
            import redis.asyncio as aioredis

            client = aioredis.from_url(self._redis_url)
            raw = await client.lpop(self._queue_name)
            await client.aclose()
            if not raw:
                return None
            data: dict[str, Any] = json.loads(raw)
            return QueuePayload(**data)
        except Exception as exc:  # pragma: no cover
            raise ConnectionError(f"出队失败: {exc}") from exc
