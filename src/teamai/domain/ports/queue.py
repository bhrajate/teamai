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
    # 执行 Agent 需要原始指令，但 tasks 表只存 intent 不存原文，
    # 故由队列载荷携带。默认空串保证旧消息仍能反序列化。
    prompt: str = ""
    tag_name: str | None = None
    thread_ref: str = ""  # 线程根引用，回复时定位目标线程


class TaskQueue(ABC):
    """长任务队列。实现方负责与 Redis/ARQ 等具体队列交互。"""

    @abstractmethod
    async def enqueue(self, payload: QueuePayload) -> None:
        """入队；队列不可用时抛 ConnectionError 由调用方处理。"""
        ...

    @abstractmethod
    async def dequeue(self, timeout_seconds: float = 0) -> QueuePayload | None:
        """弹出一个任务；无任务时返回 None。

        timeout_seconds > 0 表示阻塞等待至多这么久（实现方可用 BLPOP 之类的
        阻塞原语），期间一有任务入队就立即返回；0 表示不等待，立即返回。

        端口层暴露超时而非让调用方自己 sleep 轮询：轮询的延迟下限就是轮询
        间隔，且空转期间仍在不停打 Redis。阻塞取把「取任务的延迟」与「空转
        开销」同时降下来，而超时上限的存在保证调用方仍能周期性醒来检查停止
        信号。
        """
        ...
