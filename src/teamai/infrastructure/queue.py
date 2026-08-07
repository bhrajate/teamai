"""任务队列的 Redis 实现（TaskQueue 端口）。"""

from __future__ import annotations

import json
from typing import Any

from teamai.config import settings
from teamai.domain.ports import QueuePayload, TaskQueue
from teamai.infrastructure.redis_client import RedisClientProvider


class RedisTaskQueue(TaskQueue):
    """基于 Redis list 的队列。

    client 由共享的 RedisClientProvider 提供而非每次新建：worker 空转时每秒
    dequeue 一次，逐次建连等于零流量也每天数万次 TCP 握手。
    """

    def __init__(self, redis: RedisClientProvider | None = None, queue_name: str | None = None) -> None:
        self._queue_name = queue_name or settings.arq_queue_name
        self._redis = redis or RedisClientProvider()

    async def enqueue(self, payload: QueuePayload) -> None:
        try:
            client = self._redis.client()
            await client.rpush(self._queue_name, json.dumps(payload.__dict__))
        except Exception as exc:  # pragma: no cover - 依赖外部服务
            raise ConnectionError(f"入队失败: {exc}") from exc

    async def dequeue(self) -> QueuePayload | None:
        try:
            client = self._redis.client()
            raw = await client.lpop(self._queue_name)
            if not raw:
                return None
            data: dict[str, Any] = json.loads(raw)
            return QueuePayload(**data)
        except Exception as exc:  # pragma: no cover
            raise ConnectionError(f"出队失败: {exc}") from exc
