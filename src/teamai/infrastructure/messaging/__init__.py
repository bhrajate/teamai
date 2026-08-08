"""出向消息发送实现（MessagePublisher 端口），按平台分模块。"""

from __future__ import annotations

from teamai.infrastructure.messaging.feishu import FeishuPublisher
from teamai.infrastructure.messaging.registry import PublisherRegistry
from teamai.infrastructure.messaging.slack import SlackPublisher

__all__ = ["FeishuPublisher", "PublisherRegistry", "SlackPublisher"]
