"""领域对外部系统的抽象端口（非持久化类）。

与 repositories 同理：契约由领域层声明，infrastructure 层提供实现。
"""

from __future__ import annotations

from teamai.domain.ports.conversation import (
    MessageWindow,
    ThreadLocator,
    ThreadMessage,
    ThreadReader,
)
from teamai.domain.ports.cooldown import AmbientCooldown
from teamai.domain.ports.dedup import EventDeduplicator
from teamai.domain.ports.embedding import Embedder
from teamai.domain.ports.llm import LLMGateway, LLMResult, TokenBudgetExceeded
from teamai.domain.ports.messaging import MessagePublisher, ReplyTarget
from teamai.domain.ports.queue import QueuePayload, TaskQueue
from teamai.domain.ports.tools import ToolBundle, ToolProvider

__all__ = [
    "AmbientCooldown",
    "Embedder",
    "EventDeduplicator",
    "LLMGateway",
    "LLMResult",
    "MessagePublisher",
    "MessageWindow",
    "QueuePayload",
    "ReplyTarget",
    "TaskQueue",
    "ThreadLocator",
    "ThreadMessage",
    "ThreadReader",
    "TokenBudgetExceeded",
    "ToolBundle",
    "ToolProvider",
]
