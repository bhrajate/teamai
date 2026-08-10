"""任务队列的 Redis 实现（TaskQueue 端口）。"""

from __future__ import annotations

import json
import math
from typing import Any

from teamai.config import settings
from teamai.domain.ports import QueuePayload, TaskQueue
from teamai.infrastructure.redis_client import RedisClientProvider


class RedisTaskQueue(TaskQueue):
    """基于 Redis list 的队列：rpush 入队，blpop/lpop 出队。

    client 由共享的 RedisClientProvider 提供而非每次新建：worker 的出队是
    常驻循环，逐次建连等于零流量也不停 TCP 握手。

    ⚠️ 阻塞取会占住一条连接直到超时或有数据。当前只有一个消费协程，连接池
    够用；若将来起多个消费者共用一个 provider，须确认池容量 >= 并发消费数，
    否则别的调用（dedup 等）会排在 BLPOP 后面干等。
    """

    def __init__(self, redis: RedisClientProvider | None = None, queue_name: str | None = None) -> None:
        self._queue_name = queue_name or settings.queue_name
        self._redis = redis or RedisClientProvider()

    async def enqueue(self, payload: QueuePayload) -> None:
        try:
            client = self._redis.client()
            await client.rpush(self._queue_name, json.dumps(payload.__dict__))
        except Exception as exc:  # pragma: no cover - 依赖外部服务
            raise ConnectionError(f"入队失败: {exc}") from exc

    async def dequeue(self, timeout_seconds: float = 0) -> QueuePayload | None:
        """取一个任务。timeout_seconds > 0 走 BLPOP 阻塞等待，否则 LPOP 立即返回。

        阻塞时长会被夹到 provider.max_block_seconds 以内：客户端的 socket 读期限
        若先于 BLPOP 到点，抛的是 TimeoutError 而不是正常返回 nil。在这里夹而不是
        让调用方自觉，是因为「安全上限是多少」取决于连接配置，调用方不该知道，
        也不该在它变化时跟着改。

        超时向上取整到整秒：BLPOP 从 Redis 6.0 起支持小数秒，取整少一个对服务端
        版本的隐式要求，代价只是至多多等不到一秒才醒来。
        """
        try:
            client = self._redis.client()
            if timeout_seconds > 0:
                block = math.ceil(min(timeout_seconds, self._redis.max_block_seconds))
                # BLPOP 返回 (key, value) 二元组；超时返回 None
                got = await client.blpop([self._queue_name], timeout=block)
                raw = got[1] if got else None
            else:
                raw = await client.lpop(self._queue_name)
            if not raw:
                return None
            data: dict[str, Any] = json.loads(raw)
            return QueuePayload(**data)
        except Exception as exc:  # pragma: no cover
            raise ConnectionError(f"出队失败: {exc}") from exc
