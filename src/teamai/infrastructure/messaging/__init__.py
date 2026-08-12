"""平台消息收发实现，按平台分模块。

出向是 MessagePublisher（`*_publisher` 语义的 feishu.py / slack.py），
入向读取是 ThreadReader（`*_reader.py`）。两个端口分开的理由见
domain/ports/conversation.py 的模块注释。
"""

from __future__ import annotations

from teamai.infrastructure.messaging.feishu import FeishuPublisher
from teamai.infrastructure.messaging.feishu_reader import FeishuThreadReader
from teamai.infrastructure.messaging.reader_registry import (
    CachedThreadReader,
    ThreadReaderRegistry,
)
from teamai.infrastructure.messaging.registry import PublisherRegistry
from teamai.infrastructure.messaging.slack import SlackPublisher
from teamai.infrastructure.messaging.slack_reader import SlackThreadReader

__all__ = [
    "CachedThreadReader",
    "FeishuPublisher",
    "FeishuThreadReader",
    "PublisherRegistry",
    "SlackPublisher",
    "SlackThreadReader",
    "ThreadReaderRegistry",
]
