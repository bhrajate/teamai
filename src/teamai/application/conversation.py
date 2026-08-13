"""会话上下文服务：为 Agent 取当前线程的最近消息，并把经手的新消息回填进缓存。

为什么不自建消息表：IM 平台本身就是聊天记录的唯一权威源，镜像一份会引入
三个问题 —— 消息编辑/撤回后镜像滞后、频道 ACL 变更后镜像仍在按旧权限供数、
以及我们由此成为公司全部沟通内容的第二个数据控制者（删除权无从落实）。
完整论证见 docs/Design-conversation-context.md §2。

拉取失败一律降级为空历史：没有上下文的回答仍然可用，为拉不到历史而让整个
任务失败是不划算的。这个取舍也决定了 ThreadReader 端口不抛异常。

`note_inbound` / `note_outbound` 是缓存的自更新入口，不是第二份存储：它们只往
已经存在的缓存里补一条，无缓存时什么都不做（语义见 ThreadHistorySink）。有了
它们，机器人在 TTL 窗口内也能看见自己上一轮的回复 —— 而这正是多轮对话所需。
"""

from __future__ import annotations

import logging

from teamai.domain.models import ChannelInstance
from teamai.domain.ports import (
    ThreadHistorySink,
    ThreadLocator,
    ThreadMessage,
    ThreadReader,
)

logger = logging.getLogger(__name__)


class ConversationService:
    def __init__(
        self,
        reader: ThreadReader,
        default_limit: int = 30,
        sink: ThreadHistorySink | None = None,
    ) -> None:
        self._reader = reader
        self._default_limit = default_limit
        # 可选：无 Redis 的部署里缓存本身就不存在，此时回填无处可去。缺它只是
        # 退回「每次读都打平台」，功能不受影响。
        self._sink = sink

    @staticmethod
    def _locator(instance: ChannelInstance, thread_ref: str) -> ThreadLocator:
        return ThreadLocator(
            platform=instance.platform,
            channel_id=instance.channel_id,
            thread_ref=thread_ref,
        )

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
        locator = self._locator(instance, thread_ref)
        try:
            messages = await self._reader.fetch_thread(locator, limit or self._default_limit)
        except Exception as exc:  # pragma: no cover - 端口约定不抛，这里是兜底
            # 端口契约要求实现方自行兜住平台异常。仍在此再兜一层：某个实现
            # 违约时，代价应该是「这次没有历史」而不是整个任务失败。
            logger.warning(f"拉取线程历史失败 {instance.platform}/{thread_ref}: {exc}")
            return []
        return messages

    async def note_inbound(
        self,
        instance: ChannelInstance,
        thread_ref: str,
        author_id: str,
        text: str,
    ) -> None:
        """记下一条收到的用户消息。

        对所有入向消息调用，不只是 @ 机器人的那些：非 @ 消息同样出现在线程里，
        平台拉取时也会返回它们，漏掉会让缓存与平台不一致。
        """
        await self._note(instance, thread_ref, ThreadMessage(author_id=author_id, text=text))

    async def note_outbound(
        self,
        instance: ChannelInstance,
        thread_ref: str,
        text: str,
    ) -> None:
        """记下一条机器人发出的回复。

        `is_bot=True` 让下一轮的提示词把它渲染成 "AI:" 而非某个用户 —— 混作
        一堆无署名文本时模型容易把自己的上一轮输出当成用户诉求。
        """
        await self._note(
            instance,
            thread_ref,
            ThreadMessage(author_id=instance.agent_identity, text=text, is_bot=True),
        )

    async def _note(
        self,
        instance: ChannelInstance,
        thread_ref: str,
        message: ThreadMessage,
    ) -> None:
        """回填的共同兜底：没 sink、没锚点、没正文都直接跳过。

        异常一律吞掉并记 debug：回填是缓存的自我维护，失败的代价是这条消息在
        当前 TTL 窗口内缺席，下个窗口由平台数据重建时自然补上。为它让入向消息
        处理失败是不划算的 —— 那会连带影响建任务。
        """
        if self._sink is None or not thread_ref or not message.text:
            return
        try:
            await self._sink.note(self._locator(instance, thread_ref), message)
        except Exception as exc:  # pragma: no cover - 端口约定不抛，这里是兜底
            logger.debug(f"回填线程历史缓存失败 {instance.platform}/{thread_ref}: {exc}")
