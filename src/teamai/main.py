"""Web 进程入口：Admin API + Slack 事件入口。

长任务与定时调度在独立进程，见 teamai.worker。
应用装配（含 Slack 挂载与 lifespan）见 teamai.app。

用法：
    python -m teamai.main                          # 本进程
    uvicorn teamai.app:create_app --factory        # 交给外部 uvicorn 托管
"""

from __future__ import annotations

import logging

import uvicorn

from teamai.app import create_app
from teamai.config import settings

logger = logging.getLogger(__name__)


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    uvicorn.run(
        create_app(),
        host=settings.admin_api_host,
        port=settings.admin_api_port,
    )


if __name__ == "__main__":
    main()
