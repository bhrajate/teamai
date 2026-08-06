"""ARQ 任务队列封装：异步长任务入队/消费。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from teamai.config import settings


@dataclass
class QueuePayload:
    task_id: str
    channel_instance_id: str
    model_level: str


async def enqueue_long_task(payload: QueuePayload, queue_name: str | None = None) -> None:
    """将长任务入队。Redis 不可用时抛 ConnectionError 由调用方处理。"""
    try:
        import redis.asyncio as aioredis

        client = aioredis.from_url(settings.redis_url)
        name = queue_name or settings.arq_queue_name
        await client.rpush(name, __import__("json").dumps(payload.__dict__))
        await client.aclose()
    except Exception as exc:  # pragma: no cover - 依赖外部服务
        raise ConnectionError(f"入队失败: {exc}") from exc


async def dequeue_long_task(queue_name: str | None = None) -> QueuePayload | None:
    """从队列弹出任务；空队列返回 None。"""
    try:
        import json

        import redis.asyncio as aioredis

        client = aioredis.from_url(settings.redis_url)
        name = queue_name or settings.arq_queue_name
        raw = await client.lpop(name)
        await client.aclose()
        if not raw:
            return None
        data: dict[str, Any] = json.loads(raw)
        return QueuePayload(**data)
    except Exception as exc:  # pragma: no cover
        raise ConnectionError(f"出队失败: {exc}") from exc
