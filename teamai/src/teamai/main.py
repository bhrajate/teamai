"""应用入口：启动 Slack app + Admin API + Scheduler。"""

from __future__ import annotations

import logging

import uvicorn
from fastapi import FastAPI

from teamai.adapters.admin_api import build_admin_router
from teamai.adapters.slack_app import build_slack_app
from teamai.config import settings
from teamai.container import build_container
from teamai.infrastructure.db import init_db

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

_container = None


def get_container():
    global _container
    if _container is None:
        _container = build_container()
    return _container


async def _init() -> None:
    try:
        await init_db()
        logger.info("数据库初始化完成")
    except Exception as exc:
        logger.warning(f"数据库初始化失败（可能未启动 Postgres）: {exc}")


def create_app() -> FastAPI:
    container = get_container()
    app = FastAPI(title="TeamAI Admin API", version="0.1.0")

    @app.on_event("startup")
    async def startup() -> None:
        await _init()

    app.include_router(build_admin_router(container))
    return app


def main() -> None:
    """启动 Admin API；Slack app 通过环境变量启用时一并启动。

    用法：python -m teamai.main
    """
    import asyncio

    app = create_app()

    async def _run() -> None:
        server = uvicorn.Server(uvicorn.Config(app, host=settings.admin_api_host, port=settings.admin_api_port))
        slack_task = None
        if settings.slack_bot_token and settings.slack_signing_secret:
            slack_app = build_slack_app(get_container().router)
            if settings.slack_app_token:
                slack_task = asyncio.create_task(slack_app.async_start(socket_mode=True))
            else:
                slack_task = asyncio.create_task(slack_app.async_start())
            logger.info("Slack app 已启动")
        try:
            await server.serve()
        finally:
            if slack_task is not None:
                slack_task.cancel()

    asyncio.run(_run())


if __name__ == "__main__":
    main()
