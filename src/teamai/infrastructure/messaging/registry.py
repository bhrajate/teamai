"""按平台分发出向消息的注册表（MessagePublisher 实现）。

同步链路（web 进程直接回复）与异步链路（worker 消费长任务后回帖）共用同一个
PublisherRegistry 实例，按 target.platform 路由到对应平台实现 —— 调用方拿到
ReplyTarget（由 ChannelInstance + Task.thread_ref 拼出）即可回帖，无需关心
当前在哪个进程、哪个平台。

平台未注册时记 warning 并丢弃，不抛异常打断任务状态推进。
"""

from __future__ import annotations

import logging

from teamai.domain.ports import MessagePublisher, ReplyTarget

logger = logging.getLogger(__name__)


class PublisherRegistry(MessagePublisher):
    def __init__(self, publishers: dict[str, MessagePublisher] | None = None) -> None:
        self._publishers: dict[str, MessagePublisher] = publishers or {}

    def register(self, platform: str, publisher: MessagePublisher) -> None:
        self._publishers[platform] = publisher

    async def reply(self, target: ReplyTarget, text: str) -> None:
        publisher = self._publishers.get(target.platform)
        if publisher is None:
            logger.warning(f"平台 {target.platform} 未注册 publisher，丢弃回复")
            return
        await publisher.reply(target, text)

    async def aclose(self) -> None:
        """关闭各平台 publisher 持有的 SDK client（session / 连接池）。"""
        for publisher in self._publishers.values():
            closer = getattr(publisher, "aclose", None)
            if closer is not None:
                await closer()  # type: ignore[misc]
