"""ORM 表模型（对应领域模型映射），按聚合分模块。

⚠️ 本文件必须导入全部表模块。SQLAlchemy 只在类定义被执行时才把表注册到
`Base.metadata`，而 `init_db()` 依赖 `Base.metadata.create_all` 建表。
漏掉任何一个 import，对应的表就会静默地不被创建。新增表模块时记得补这里。
"""

from __future__ import annotations

from teamai.infrastructure.orm.audit import AuditLogModel
from teamai.infrastructure.orm.budget import BudgetQuotaModel
from teamai.infrastructure.orm.channel import ChannelInstanceModel
from teamai.infrastructure.orm.interaction import AgentInteractionModel
from teamai.infrastructure.orm.memory import MemoryEntryModel, PreferenceModel
from teamai.infrastructure.orm.policy import PolicyModel
from teamai.infrastructure.orm.tag import TagTemplateModel
from teamai.infrastructure.orm.task import TaskModel

__all__ = [
    "AgentInteractionModel",
    "AuditLogModel",
    "BudgetQuotaModel",
    "ChannelInstanceModel",
    "MemoryEntryModel",
    "PolicyModel",
    "PreferenceModel",
    "TagTemplateModel",
    "TaskModel",
]
