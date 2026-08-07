"""ASGI 应用装配：Admin API + Slack 入口，共用一个事件循环。

进程边界的划分理由见 worker.py 顶部说明。这里只负责「对外收请求」这一件事：

- Admin API 路由（FastAPI）
- Slack 事件入口。两种模式二选一，由配置决定：
  * 配了 slack_app_token → Socket Mode，出方向 WS 长连接，挂 lifespan 后台任务
  * 未配 → Events API，挂到 POST /slack/events，复用同一个 uvicorn 端口

Slack 不再单独起服务器：slack-bolt 的 AsyncApp.start() 会拉起一个自带的 aiohttp
服务器占用另一个端口，与 uvicorn 并存后健康检查、日志、优雅退出都要维护两套。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request

from teamai.adapters.admin_api import build_admin_router
from teamai.config import settings
from teamai.container import Container, build_container

logger = logging.getLogger(__name__)

_container: Container | None = None


def get_container() -> Container:
    """进程内共享的组合根。首次调用时装配。"""
    global _container
    if _container is None:
        _container = build_container()
    return _container


def reset_container() -> None:
    """丢弃已装配的容器。供测试隔离用。"""
    global _container
    _container = None


def slack_enabled() -> bool:
    """Slack 凭据是否齐备。缺任一项则只跑 Admin API。"""
    return bool(settings.slack_bot_token and settings.slack_signing_secret)


async def init_database() -> None:
    """建表。失败只告警不中断 —— Admin API 的 /health 仍应可用于探活。"""
    from teamai.infrastructure.db import init_db

    try:
        await init_db()
        logger.info("数据库初始化完成")
    except Exception as exc:
        logger.warning(f"数据库初始化失败（可能未启动 Postgres）: {exc}")


def create_app() -> FastAPI:
    container = get_container()

    slack_app = None
    socket_handler = None
    if slack_enabled():
        from teamai.adapters.slack_app import build_slack_app

        slack_app = build_slack_app(container.router)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        nonlocal socket_handler
        await init_database()

        socket_task: asyncio.Task[None] | None = None
        if slack_app is not None and settings.slack_app_token:
            from teamai.adapters.slack_app import build_socket_mode_handler

            socket_handler = build_socket_mode_handler(slack_app)
            socket_task = asyncio.create_task(socket_handler.start_async(), name="slack-socket-mode")
            logger.info("Slack Socket Mode 已连接")
        elif slack_app is not None:
            logger.info("Slack Events API 已挂载于 POST /slack/events")
        else:
            logger.info("未配置 Slack 凭据，仅启动 Admin API")

        try:
            yield
        finally:
            if socket_handler is not None:
                try:
                    await socket_handler.close_async()
                except Exception as exc:  # pragma: no cover - 退出路径尽力而为
                    logger.warning(f"Socket Mode 关闭异常: {exc}")
            if socket_task is not None:
                socket_task.cancel()
                # 等取消真正生效，否则 uvicorn 退出时会报 pending task
                await asyncio.gather(socket_task, return_exceptions=True)
                logger.info("Slack Socket Mode 已断开")

    app = FastAPI(title="TeamAI Admin API", version="0.1.0", lifespan=lifespan)
    app.include_router(build_admin_router(container))

    # Events API 模式下才需要 HTTP 入口；Socket Mode 走 WS，挂了也收不到事件
    if slack_app is not None and not settings.slack_app_token:
        from teamai.adapters.slack_app import build_events_handler

        events_handler = build_events_handler(slack_app)

        @app.post("/slack/events")
        async def slack_events(request: Request):  # type: ignore[no-untyped-def]
            return await events_handler.handle(request)

    return app
