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
from teamai.domain.ports import EventDeduplicator

if TYPE_CHECKING:
    # 两个 handler 的真实导入刻意留在函数内：Socket Mode 与 Events API 是互斥的
    # 接入方式，各自的 adapter 会拉起不同依赖，按需导入避免用一种时加载另一种。
    from slack_bolt.adapter.fastapi.async_handler import AsyncSlackRequestHandler
    from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler


def dedup_key(body: dict) -> str:
    """从 Slack 请求信封取去重键。

    优先用信封里的 `event_id`：Slack 为每个事件分配一次，重投时保持不变，
    是官方指定的去重依据（Events API 与 Socket Mode 的 body 都带它）。

    取不到才退回 `channel:ts:subtype` 拼装 —— 这个组合能标识「哪条消息」，
    但同一条消息的多次重投也只会得到同一个键，故仍可用；只是遇上编辑消息
    等 ts 相同而内容不同的场景会误判，所以只作兜底。
    """
    event_id = str(body.get("event_id", ""))
    if event_id:
        return event_id
    event = body.get("event", {}) or {}
    return f"{event.get('channel', '')}:{event.get('ts', '')}:{event.get('subtype', '')}"


def build_slack_app(router: MessageRouter, dedup: EventDeduplicator) -> AsyncApp:
    """构建 Slack AsyncApp 并注册事件处理器。

    dedup 在两个处理器的最前面拦重投事件，避免一条 @提及被重复建任务、
    重复调 LLM、重复回复。
    """
    app = AsyncApp(token=settings.slack_bot_token, signing_secret=settings.slack_signing_secret)

    @app.event("app_mention")
    async def on_mention(event: dict, body: dict, say, logger):  # type: ignore[no-untyped-def]
        key = dedup_key(body)
        if await dedup.is_duplicate(key):
            logger.info(f"忽略重投的 app_mention 事件: {key}")
            return
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
    async def on_message(event: dict, body: dict, say, logger):  # type: ignore[no-untyped-def]
        if event.get("subtype") in ("bot_message",):
            return
        key = dedup_key(body)
        if await dedup.is_duplicate(key):
            logger.info(f"忽略重投的 message 事件: {key}")
            return
        channel = event.get("channel", "")
        ts = event.get("ts", "")
        user = event.get("user", "")
        text = event.get("text", "")
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
