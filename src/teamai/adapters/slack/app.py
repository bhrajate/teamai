"""Slack 适配层：slack-bolt AsyncApp 装配与两种接入方式。

Slack 有两条互斥的事件通道：
- Socket Mode：出方向 WebSocket 长连接，不监听端口，适合内网/无公网回调场景
- Events API：Slack 发 HTTP webhook 进来，需要公网可达的 URL

本模块两者都提供构建函数，但都不自己起服务器 —— 进程生命周期由
SlackConnector 统管（进程入口遍历连接器），避免 slack-bolt 自带的 aiohttp
服务器与 uvicorn 各占一个端口。

平台无关化后，处理器把事件经 translator 归一成 IncomingMessage 再交给
router.route()；同步回复仍走 slack-bolt 的 say()（少一次往返）。
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from fastapi import FastAPI, Request, Response
from slack_bolt.async_app import AsyncApp

from teamai.adapters.base import PlatformConnector
from teamai.adapters.slack.translator import event_to_incoming
from teamai.application.router import MessageRouter
from teamai.config import settings
from teamai.domain.ports import EventDeduplicator

if TYPE_CHECKING:
    # 两个 handler 的真实导入刻意留在函数内：Socket Mode 与 Events API 是互斥的
    # 接入方式，各自的 adapter 会拉起不同依赖，按需导入避免用一种时加载另一种。
    from slack_bolt.adapter.fastapi.async_handler import AsyncSlackRequestHandler
    from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler

logger = logging.getLogger(__name__)


def build_slack_app(router: MessageRouter, dedup: EventDeduplicator) -> AsyncApp:
    """构建 Slack AsyncApp 并注册事件处理器。

    dedup 在两个处理器的最前面拦重投事件，避免一条 @提及被重复建任务、
    重复调 LLM、重复回复。
    """
    app = AsyncApp(token=settings.slack_bot_token, signing_secret=settings.slack_signing_secret)

    @app.event("app_mention")
    async def on_mention(event: dict, body: dict, say, logger):  # type: ignore[no-untyped-def]
        msg = event_to_incoming(event, body, is_mention=True)
        if await dedup.is_duplicate(msg.event_id):
            logger.info(f"忽略重投的 app_mention 事件: {msg.event_id}")
            return
        try:
            decision = await router.route(msg)
            await say(text=decision.message or "任务已受理", thread_ts=msg.thread_ref)
        except Exception as exc:  # pragma: no cover
            logger.error(f"app_mention 处理失败: {exc}")
            await say(text="抱歉，任务处理出错，请稍后重试。", thread_ts=msg.thread_ref)

    @app.message()
    async def on_message(event: dict, body: dict, say, logger):  # type: ignore[no-untyped-def]
        if event.get("subtype") in ("bot_message",):
            return
        msg = event_to_incoming(event, body, is_mention=False)
        if await dedup.is_duplicate(msg.event_id):
            logger.info(f"忽略重投的 message 事件: {msg.event_id}")
            return
        try:
            await router.route(msg)
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


class SlackConnector(PlatformConnector):
    """Slack 连接器：把两种接入方式收进统一生命周期。

    mode 取 `platforms_slack_mode`（events | socket | auto），auto 保持既有
    行为：配了 `slack_app_token` 走 Socket Mode，否则走 Events API。
    显式给 mode 是为与飞书对齐 —— 飞书两种模式所需凭据有重叠，隐式推断会歧义，
    Slack 侧保留 auto 兼容旧配置。
    """

    name = "slack"

    def __init__(self, router: MessageRouter, dedup: EventDeduplicator) -> None:
        self._app = build_slack_app(router, dedup)
        self._socket_handler: AsyncSocketModeHandler | None = None
        self._socket_task: asyncio.Task[None] | None = None
        self._events_handler: AsyncSlackRequestHandler | None = None

    def _mode(self) -> str:
        cfg = settings.platforms_slack_mode
        if cfg in ("events", "socket"):
            return cfg
        return "socket" if settings.slack_app_token else "events"

    def mount(self, app: FastAPI) -> None:
        """Events API 模式挂 HTTP 入口；Socket Mode 走 WS，挂了也收不到事件。"""
        if self._mode() == "socket":
            return
        self._events_handler = build_events_handler(self._app)

        @app.post("/slack/events")
        async def slack_events(request: Request) -> Response:
            return await self._events_handler.handle(request)

    async def startup(self) -> None:
        if self._mode() == "socket":
            self._socket_handler = build_socket_mode_handler(self._app)
            self._socket_task = asyncio.create_task(
                self._socket_handler.start_async(), name="slack-socket-mode"
            )
            logger.info("Slack Socket Mode 已连接")
        else:
            logger.info("Slack Events API 已挂载于 POST /slack/events")

    async def shutdown(self) -> None:
        """断开长连接并回收后台任务，退出时不残留 pending task。"""
        if self._socket_handler is not None:
            try:
                await self._socket_handler.close_async()
            except Exception as exc:  # pragma: no cover - 退出路径尽力而为
                logger.warning(f"Socket Mode 关闭异常: {exc}")
        if self._socket_task is not None:
            self._socket_task.cancel()
            # 等取消真正生效，否则 uvicorn 退出时会报 pending task
            await asyncio.gather(self._socket_task, return_exceptions=True)
            logger.info("Slack Socket Mode 已断开")
