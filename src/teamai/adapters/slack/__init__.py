"""Slack 适配子包。对外只暴露 build_connector，进程入口不再感知接入模式。

低层构建函数（build_slack_app 等）是 SlackConnector 的内部实现，
不再从包级导出。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from teamai.adapters.base import PlatformConnector
from teamai.adapters.slack.app import SlackConnector
from teamai.config import settings

if TYPE_CHECKING:
    from teamai.container import Container


def build_connector(container: Container) -> PlatformConnector | None:
    """凭据齐备才接入 Slack；缺凭据返回 None，进程入口据此跳过。"""
    if not settings.slack_enabled:
        return None
    return SlackConnector(container.router, container.dedup)


__all__ = ["build_connector"]
