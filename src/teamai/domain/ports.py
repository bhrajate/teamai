"""领域对外部系统的抽象端口（非持久化类）。

与 repositories.py 同理：契约由领域层声明，infrastructure 层提供实现。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class QueuePayload:
    task_id: str
    channel_instance_id: str
    model_level: str


class TaskQueue(ABC):
    """长任务队列。实现方负责与 Redis/ARQ 等具体队列交互。"""

    @abstractmethod
    async def enqueue(self, payload: QueuePayload) -> None:
        """入队；队列不可用时抛 ConnectionError 由调用方处理。"""
        ...

    @abstractmethod
    async def dequeue(self) -> QueuePayload | None:
        """弹出一个任务；空队列返回 None。"""
        ...
