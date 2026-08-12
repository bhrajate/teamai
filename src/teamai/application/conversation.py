"""会话上下文服务：为 Agent 取当前线程的最近消息。

为什么不自建消息表：IM 平台本身就是聊天记录的唯一权威源，镜像一份会引入
三个问题 —— 消息编辑/撤回后镜像滞后、频道 ACL 变更后镜像仍在按旧权限供数、
以及我们由此成为公司全部沟通内容的第二个数据控制者（删除权无从落实）。
完整论证见 docs/Design-conversation-context.md §2。

拉取失败一律降级为空历史：没有上下文的回答仍然可用，为拉不到历史而让整个
任务失败是不划算的。这个取舍也决定了 ThreadReader 端口不抛异常。
"""

from __future__ import annotations

import logging

from teamai.domain.models import ChannelInstance
from teamai.domain.ports import ThreadLocator, ThreadMessage, ThreadReader

logger = logging.getLogger(__name__)


class ConversationService:
    def __init__(self, reader: ThreadReader, default_limit: int = 30) -> None:
        self._reader = reader
        self._default_limit = default_limit

    async def thread_history(
        self,
        instance: ChannelInstance,
        thread_ref: str,
        limit: int | None = None,
    ) -> list[ThreadMessage]:
        """取该线程的最近消息，按时间正序。

        `thread_ref` 为空时直接返回空：那意味着调用方手里没有线程锚点
        （例如某些系统触发的任务），拉取无从下手。
        """
        if not thread_ref:
            return []
        locator = ThreadLocator(
            platform=instance.platform,
            channel_id=instance.channel_id,
            thread_ref=thread_ref,
        )
        try:
            messages = await self._reader.fetch_thread(locator, limit or self._default_limit)
        except Exception as exc:  # pragma: no cover - 端口约定不抛，这里是兜底
            # 端口契约要求实现方自行兜住平台异常。仍在此再兜一层：某个实现
            # 违约时，代价应该是「这次没有历史」而不是整个任务失败。
            logger.warning(f"拉取线程历史失败 {instance.platform}/{thread_ref}: {exc}")
            return []
        return messages
