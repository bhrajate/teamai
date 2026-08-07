"""Slack 适配层：slack-bolt AsyncApp 装配与两种接入方式。

Slack 有两条互斥的事件通道：
- Socket Mode：出方向 WebSocket 长连接，不监听端口，适合内网/无公网回调场景
- Events API：Slack 发 HTTP webhook 进来，需要公网可达的 URL

本模块两者都提供构建函数，但都不自己起服务器 —— 进程生命周期由 web 进程入口
（app/backend/main.py）统管，避免 slack-bolt 自带的 aiohttp 服务器与 uvicorn 各占一个端口。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from slack_bolt.async_app import AsyncApp

from teamai.application.router import MessageRouter
from teamai.config import settings

if TYPE_CHECKING:
    # 两个 handler 的真实导入刻意留在函数内：Socket Mode 与 Events API 是互斥的
    # 接入方式，各自的 adapter 会拉起不同依赖，按需导入避免用一种时加载另一种。
    from slack_bolt.adapter.fastapi.async_handler import AsyncSlackRequestHandler
    from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler


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


def build_socket_mode_handler(app: AsyncApp) -> AsyncSocketModeHandler:
    """构建 Socket Mode 处理器。

    调用方用 `await handler.start_async()` 建立长连接（会一直阻塞，需放后台任务），
    用 `await handler.close_async()` 断开。
    """
    from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler

    return AsyncSocketModeHandler(app, settings.slack_app_token)


def build_events_handler(app: AsyncApp) -> AsyncSlackRequestHandler:
    """构建 Events API 处理器，供 FastAPI 路由转发请求。

    用法：`await handler.handle(request)`，签名校验由 slack-bolt 内部完成。
    """
    from slack_bolt.adapter.fastapi.async_handler import AsyncSlackRequestHandler

    return AsyncSlackRequestHandler(app)
