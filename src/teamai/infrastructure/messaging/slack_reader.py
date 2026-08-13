"""Slack 线程读取（ThreadReader 实现）。

`conversations.replies` 与我们的 thread_ref 语义直接对应：thread_ref 取的就是
`thread_ts or ts`（见 adapters/slack/translator.py），正是该接口要的 ts。

⚠️ 速率限制：Slack 近年收紧了非 Marketplace 应用对 conversations 系接口的配额，
具体额度取决于应用类型与上架状态。故本读取一定要配合 CachedThreadReader 使用，
否则同一线程里连续几条消息会各打一次 API。真被限流时这里只记 warning 并返回
空历史，任务照跑。

`is_self` 的判定需要本 bot 自己的 id，由 `auth.test` 取一次后缓存。它同时返回
`bot_id`（形如 `B0123`，正是 bot 消息里带的那个字段）与 `user_id`（bot 用户），
两者都比对：经 `chat.postMessage` 发出的消息通常两个字段都有，只比一个会漏。
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
    def __init__(
        self,
        client: AsyncWebClient | None = None,
        token: str = "",
        bot_id: str = "",
        bot_user_id: str = "",
    ) -> None:
        self._client = client or AsyncWebClient(token=token or settings.slack_bot_token)
        # 显式传入则不打 auth.test（测试与已知身份的部署走这条）。
        self._bot_id = bot_id
        self._bot_user_id = bot_user_id
        # 区分「还没问过」与「问过且失败」：失败也不再重试，否则每次拉线程都白打
        # 一次 auth.test（权限不足是稳定失败，重试不会变好）。
        self._identity_resolved = bool(bot_id or bot_user_id)

    async def _resolve_identity(self) -> None:
        """取本 bot 的 id，仅首次调用时打一次 auth.test。

        失败不抛：拿不到身份只会让 `is_self` 全为假（自己的历史回复被当成普通
        参与者），比让整条线程拉取失败轻。
        """
        if self._identity_resolved:
            return
        self._identity_resolved = True
        try:
            resp = await self._client.auth_test()
            self._bot_id = resp.get("bot_id") or ""
            self._bot_user_id = resp.get("user_id") or ""
            logger.info(f"Slack bot 身份: bot_id={self._bot_id} user_id={self._bot_user_id}")
        except Exception as exc:
            logger.warning(f"Slack bot 身份拉取失败，自己的历史回复将标不出来: {exc}")

    def _is_self(self, m: dict) -> bool:
        """严格判定「这条是本 bot 发的」。

        不用 `bool(m.get("bot_id"))`：那是「某个机器人发的」，频道里的 CI 通知、
        告警机器人都会命中，于是它们的消息被渲染成 `AI:`，模型会以为那些话是
        自己上一轮说的。身份未知时一律返回 False —— 宁可把自己的回复降级成普通
        参与者，也不能把别人的话认领成自己的。
        """
        if self._bot_id and m.get("bot_id") == self._bot_id:
            return True
        return bool(self._bot_user_id and m.get("user") == self._bot_user_id)

    async def fetch_thread(self, locator: ThreadLocator, limit: int) -> list[ThreadMessage]:
        await self._resolve_identity()
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
                    is_self=self._is_self(m),
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
