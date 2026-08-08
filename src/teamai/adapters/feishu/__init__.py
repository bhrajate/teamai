"""飞书适配子包。对外只暴露 build_connector。"""

from __future__ import annotations

from typing import TYPE_CHECKING

from teamai.adapters.base import PlatformConnector
from teamai.adapters.feishu.connector import FeishuConnector
from teamai.config import settings

if TYPE_CHECKING:
    from teamai.container import Container


def build_connector(container: Container) -> PlatformConnector | None:
    """凭据齐备才接入飞书；缺凭据返回 None，进程入口据此跳过。"""
    if not settings.feishu_enabled:
        return None
    return FeishuConnector(container.router, container.dedup, container.publisher)


__all__ = ["build_connector"]
