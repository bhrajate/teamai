"""仓储实现（SQLAlchemy），按聚合分模块。

与 teamai.domain.repositories 中的抽象一一对应。领域→表的 mapper 与
对应的仓储放在同一文件，改一处字段时不必跨文件跳转。
"""

from __future__ import annotations

from teamai.infrastructure.repositories.audit import SQLAuditRepository
from teamai.infrastructure.repositories.budget import SQLBudgetRepository
from teamai.infrastructure.repositories.channel import SQLChannelRepository
from teamai.infrastructure.repositories.memory import SQLMemoryRepository
from teamai.infrastructure.repositories.policy import SQLPolicyRepository
from teamai.infrastructure.repositories.tag import SQLTagRepository
from teamai.infrastructure.repositories.task import SQLTaskRepository

__all__ = [
    "SQLAuditRepository",
    "SQLBudgetRepository",
    "SQLChannelRepository",
    "SQLMemoryRepository",
    "SQLPolicyRepository",
    "SQLTagRepository",
    "SQLTaskRepository",
]
