"""ORM 表模型（对应领域模型映射），按聚合分模块。

⚠️ 本文件必须导入全部表模块。SQLAlchemy 只在类定义被执行时才把表注册到
`Base.metadata`，而 alembic 的 env.py 以 `Base.metadata` 为 target_metadata
（建库统一走 alembic，见 db.py 的 init_db）。
漏掉任何一个 import，autogenerate / upgrade 就会静默漏掉对应表。新增表模块时记得补这里。
"""

from __future__ import annotations

from teamai.infrastructure.orm.audit import AuditLogModel
from teamai.infrastructure.orm.budget import BudgetQuotaModel
from teamai.infrastructure.orm.channel import ChannelInstanceModel
from teamai.infrastructure.orm.checkpoint import TaskCheckpointModel
from teamai.infrastructure.orm.interaction import AgentInteractionModel
from teamai.infrastructure.orm.mcp import McpServerModel
from teamai.infrastructure.orm.memory import MemoryEntryModel
from teamai.infrastructure.orm.outbox import MemoryOutboxModel
from teamai.infrastructure.orm.policy import PolicyModel
from teamai.infrastructure.orm.skill import ChannelSkillModel, SkillFileModel, SkillModel
from teamai.infrastructure.orm.tag import TagTemplateModel
from teamai.infrastructure.orm.task import TaskModel

__all__ = [
    "AgentInteractionModel",
    "AuditLogModel",
    "BudgetQuotaModel",
    "ChannelInstanceModel",
    "ChannelSkillModel",
    "McpServerModel",
    "MemoryEntryModel",
    "MemoryOutboxModel",
    "PolicyModel",
    "SkillFileModel",
    "SkillModel",
    "TagTemplateModel",
    "TaskCheckpointModel",
    "TaskModel",
]
