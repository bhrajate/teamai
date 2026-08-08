"""Slack 出向消息发送（MessagePublisher 实现）。

用 slack_sdk 的 AsyncWebClient 而非 slack-bolt：这里只负责「发一条回复」，
不需要 bolt 的事件/中间件框架；且 worker 进程没有 bolt 的 AsyncApp 实例，
publisher 是同步/异步两条链路共用的发送出口，须能独立于 web 进程装配。
"""

from __future__ import annotations

from slack_sdk.web.async_client import AsyncWebClient

from teamai.config import settings
from teamai.domain.ports import MessagePublisher, ReplyTarget


class SlackPublisher(MessagePublisher):
    def __init__(self, client: AsyncWebClient | None = None, token: str = "") -> None:
        self._client = client or AsyncWebClient(token=token or settings.slack_bot_token)

    async def reply(self, target: ReplyTarget, text: str) -> None:
        try:
            await self._client.chat_postMessage(
                channel=target.channel_id,
                text=text,
                # thread_ref 即 thread_ts（Slack 侧取值规则），空串时回频道不挂线程
                thread_ts=target.thread_ref or None,
            )
        except Exception as exc:  # pragma: no cover - 依赖外部服务
            raise ConnectionError(f"Slack 回复失败: {exc}") from exc

    async def aclose(self) -> None:
        """关闭调用方传入的 aiohttp session；未传则 no-op。

        AsyncWebClient 默认每次请求自建 session、用完即关（slack_sdk 的
        _request_with_session），不存在长期存活的连接需要收尾；只有显式传入
        session 复用时才由调用方负责关闭。
        """
        session = getattr(self._client, "session", None)
        if session is not None:
            await session.close()
