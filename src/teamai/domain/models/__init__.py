"""领域模型：实体、值对象与枚举。

按业务概念分模块，本文件汇总导出，调用方可写
`from teamai.domain.models import Task, TaskStatus`。
"""

from __future__ import annotations

from teamai.domain.models.audit import GLOBAL_SCOPE, AuditAction, AuditLog, AuditResult
from teamai.domain.models.budget import (
    BudgetPeriod,
    BudgetQuota,
    BudgetScope,
    BudgetState,
)
from teamai.domain.models.channel import ChannelInstance
from teamai.domain.models.interaction import AgentInteraction, InteractionResult
from teamai.domain.models.mcp import McpServer
from teamai.domain.models.memory import MemoryEntry, MemorySource, MemoryType, should_embed
from teamai.domain.models.outbox import OutboxEntry, OutboxOp
from teamai.domain.models.policy import AmbientRule, PermissionPolicy
from teamai.domain.models.skill import Skill, SkillFile
from teamai.domain.models.tag import TagTemplate
from teamai.domain.models.task import InvalidTransition, Task, TaskStatus

__all__ = [
    "AgentInteraction",
    "AmbientRule",
    "AuditAction",
    "AuditLog",
    "AuditResult",
    "BudgetPeriod",
    "BudgetQuota",
    "BudgetScope",
    "BudgetState",
    "ChannelInstance",
    "GLOBAL_SCOPE",
    "InteractionResult",
    "InvalidTransition",
    "McpServer",
    "MemoryEntry",
    "MemorySource",
    "MemoryType",
    "OutboxEntry",
    "OutboxOp",
    "should_embed",
    "PermissionPolicy",
    "Skill",
    "SkillFile",
    "Task",
    "TaskStatus",
    "TagTemplate",
]
