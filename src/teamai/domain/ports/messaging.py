"""出向消息发送端口。

入向由各平台 translator 归一成 IncomingMessage（application/events.py），
出向经本端口回发：同步链路（web 进程直接回复）与异步链路（worker 消费长任务后
回帖）共用同一抽象，由注册表按 platform 分发到具体平台实现。

契约由领域层声明、infrastructure 层实现，与 queue / dedup 同风格；
只依赖标准库，满足 test_domain_不导入三方库。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass(frozen=True)
class ReplyTarget:
    """回复目标。可由 ChannelInstance（platform + channel_id）与 Task.thread_ref 拼出。"""

    platform: str
    channel_id: str
    thread_ref: str


class MessagePublisher(ABC):
    """按平台把文案回复到指定线程。"""

    @abstractmethod
    async def reply(self, target: ReplyTarget, text: str) -> None:
        """在线程内回复。平台不可用时抛 ConnectionError 由调用方兜底。"""
        ...
