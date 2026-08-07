"""领域对外部系统的抽象端口（非持久化类）。

与 repositories 同理：契约由领域层声明，infrastructure 层提供实现。
"""

from __future__ import annotations

from teamai.domain.ports.dedup import EventDeduplicator
from teamai.domain.ports.queue import QueuePayload, TaskQueue

__all__ = ["EventDeduplicator", "QueuePayload", "TaskQueue"]
