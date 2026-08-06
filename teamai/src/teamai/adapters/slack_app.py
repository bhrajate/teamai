"""Slack 适配层：slack-bolt AsyncApp 装配。"""

from __future__ import annotations

from slack_bolt.async_app import AsyncApp

from teamai.application.router import MessageRouter
from teamai.config import settings


def build_slack_app(router: MessageRouter) -> AsyncApp:
    """构建 Slack AsyncApp 并注册事件处理器。"""
    app = AsyncApp(token=settings.slack_bot_token, signing_secret=settings.slack_signing_secret)

    @app.event("app_mention")
    async def on_mention(event: dict, say, logger):  # type: ignore[no-untyped-def]
        channel = event.get("channel", "")
        ts = event.get("ts", "")
        user = event.get("user", "")
        text = event.get("text", "")
        try:
            decision = await router.route(
                platform="slack",
                workspace_id=event.get("team", ""),
                channel_id=channel,
                thread_ts=ts,
                user_id=user,
                text=text,
                is_mention=True,
            )
            await say(text=decision.message or "任务已受理", thread_ts=ts)
        except Exception as exc:  # pragma: no cover
            logger.error(f"app_mention 处理失败: {exc}")
            await say(text="抱歉，任务处理出错，请稍后重试。", thread_ts=ts)

    @app.message()
    async def on_message(event: dict, say, logger):  # type: ignore[no-untyped-def]
        channel = event.get("channel", "")
        ts = event.get("ts", "")
        user = event.get("user", "")
        text = event.get("text", "")
        if event.get("subtype") in ("bot_message",):
            return
        try:
            await router.route(
                platform="slack",
                workspace_id=event.get("team", ""),
                channel_id=channel,
                thread_ts=ts,
                user_id=user,
                text=text,
                is_mention=False,
            )
        except Exception as exc:  # pragma: no cover
            logger.error(f"message 处理失败: {exc}")

    return app
