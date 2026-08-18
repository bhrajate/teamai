"""Web 进程入口：装配 ASGI 应用并起服务器。

进程边界的划分理由见 app/worker/main.py 顶部说明。这里只负责「对外收请求」这一件事：

- Admin API 路由（FastAPI）
- 各平台连接器（Slack / 飞书），接入方式由连接器自理：HTTP 回调经 mount()
  挂到 Admin API 的端口，长连接经 lifespan 里的 startup()/shutdown() 建立与回收。

新增平台只须把 build_connector 登记进 CONNECTOR_BUILDERS，本文件不再改动。

用法：
    python -m app.backend.main                          # 本进程
    uvicorn app.backend.main:create_app --factory       # 交给外部 uvicorn 托管
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from teamai.adapters.admin import build_admin_router
from teamai.adapters.feishu import build_connector as build_feishu_connector
from teamai.adapters.slack import build_connector as build_slack_connector
from teamai.config import settings
from teamai.container import get_container
from teamai.infrastructure.db import init_db_or_warn
from teamai.infrastructure.metrics import build_metrics_asgi_app

logger = logging.getLogger(__name__)

# 接入新平台时在此登记：build_connector(container) -> PlatformConnector | None，
# 凭据不全返回 None 即不接入。
CONNECTOR_BUILDERS: tuple = (build_slack_connector, build_feishu_connector)


def create_app() -> FastAPI:
    container = get_container()

    connectors = [c for b in CONNECTOR_BUILDERS if (c := b(container)) is not None]

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        await init_db_or_warn()

        for c in connectors:
            await c.startup()

        try:
            yield
        finally:
            for c in reversed(connectors):
                try:
                    await c.shutdown()
                except Exception as exc:  # pragma: no cover - 退出路径尽力而为
                    logger.warning(f"{c.name} 连接器关闭异常: {exc}")
            # 共享 Redis 连接池等长期存活的连接，须显式关闭
            try:
                await container.aclose()
            except Exception as exc:  # pragma: no cover - 退出路径尽力而为
                logger.warning(f"释放容器资源异常: {exc}")

    app = FastAPI(title="TeamAI Admin API", version="0.1.0", lifespan=lifespan)

    # 控制台前端独立部署时是另一个源，不加这个浏览器会拦掉全部 /api 请求。
    # 不用通配 "*"：allow_credentials 与 "*" 组合会被浏览器拒绝，且 Admin API
    # 上挂着可写端点，来源就该显式列举。留空即不装中间件（同源或走 vite proxy）。
    if origins := settings.admin_api_cors_origin_list:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=origins,
            allow_credentials=True,
            allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
            allow_headers=["Authorization", "Content-Type"],
        )

    app.include_router(build_admin_router(container))

    # /metrics 有意留在 Admin 令牌保护之外，与 /health 一致：抓取端通常是
    # Prometheus 而非人，让它带业务令牌既不方便也扩大了令牌的分发面。
    # ⚠️ 这个端点会暴露频道数量级、投影积压等运营信息，生产部署应在反向代理层
    # 限制来源（deploy/nginx.conf.example 里给了示例）。
    app.mount("/metrics", build_metrics_asgi_app())

    for c in connectors:
        c.mount(app)

    return app


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    uvicorn.run(
        create_app(),
        host=settings.admin_api_host,
        port=settings.admin_api_port,
    )


if __name__ == "__main__":
    main()
