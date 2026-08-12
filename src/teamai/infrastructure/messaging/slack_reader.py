"""Slack 线程读取（ThreadReader 实现）。

`conversations.replies` 与我们的 thread_ref 语义直接对应：thread_ref 取的就是
`thread_ts or ts`（见 adapters/slack/translator.py），正是该接口要的 ts。

⚠️ 速率限制：Slack 近年收紧了非 Marketplace 应用对 conversations 系接口的配额，
具体额度取决于应用类型与上架状态。故本读取一定要配合 CachedThreadReader 使用，
否则同一线程里连续几条消息会各打一次 API。真被限流时这里只记 warning 并返回
空历史，任务照跑。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from slack_sdk.web.async_client import AsyncWebClient

from teamai.config import settings
from teamai.domain.ports import ThreadLocator, ThreadMessage, ThreadReader

logger = logging.getLogger(__name__)


def _to_datetime(ts: str) -> datetime | None:
    """Slack 的 ts 是 "秒.微秒" 形式的字符串。"""
    try:
        return datetime.fromtimestamp(float(ts), tz=UTC)
    except (TypeError, ValueError):
        return None


class SlackThreadReader(ThreadReader):
    def __init__(self, client: AsyncWebClient | None = None, token: str = "") -> None:
        self._client = client or AsyncWebClient(token=token or settings.slack_bot_token)

    async def fetch_thread(self, locator: ThreadLocator, limit: int) -> list[ThreadMessage]:
        try:
            resp = await self._client.conversations_replies(
                channel=locator.channel_id,
                ts=locator.thread_ref,
                limit=limit,
            )
        except Exception as exc:
            # 端口契约：拉不到就空着。线程被删、bot 不在频道、限流都会走到这里。
            logger.warning(f"Slack 线程拉取失败 {locator.channel_id}/{locator.thread_ref}: {exc}")
            return []

        messages = resp.get("messages") or []
        out: list[ThreadMessage] = []
        for m in messages:
            text = (m.get("text") or "").strip()
            if not text:
                continue  # 纯附件/纯 block 消息没有可用文本
            out.append(
                ThreadMessage(
                    author_id=m.get("user") or m.get("bot_id") or "",
                    text=text,
                    ts=_to_datetime(m.get("ts", "")),
                    # bot_id 存在即为机器人发的（含本 bot 自己的历史回复）
                    is_bot=bool(m.get("bot_id")) or m.get("subtype") == "bot_message",
                )
            )
        # conversations.replies 已按时间正序返回，仍显式截尾：limit 是「最近 N 条」，
        # 而该接口的 limit 是分页大小，语义不完全等同。
        return out[-limit:]

    async def aclose(self) -> None:
        """理由同 SlackPublisher.aclose：只有调用方显式传入 session 时才需收尾。"""
        session = getattr(self._client, "session", None)
        if session is not None:
            await session.close()
