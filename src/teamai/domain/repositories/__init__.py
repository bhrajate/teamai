"""仓储抽象接口。

依赖倒置：由领域层声明持久化契约，infrastructure 层提供实现。
应用层只 import 本包，不感知具体 DB。
"""

from __future__ import annotations

from teamai.domain.repositories.audit import AuditRepository
from teamai.domain.repositories.budget import BudgetRepository
from teamai.domain.repositories.channel import ChannelRepository
from teamai.domain.repositories.interaction import InteractionRepository
from teamai.domain.repositories.mcp import McpServerRepository
from teamai.domain.repositories.memory import MemoryRepository
from teamai.domain.repositories.outbox import OutboxRepository, OutboxStats
from teamai.domain.repositories.policy import PolicyRepository
from teamai.domain.repositories.skill import SkillRepository
from teamai.domain.repositories.tag import TagRepository
from teamai.domain.repositories.task import TaskRepository

__all__ = [
    "AuditRepository",
    "BudgetRepository",
    "ChannelRepository",
    "InteractionRepository",
    "McpServerRepository",
    "MemoryRepository",
    "OutboxRepository",
    "OutboxStats",
    "PolicyRepository",
    "SkillRepository",
    "TagRepository",
    "TaskRepository",
]
