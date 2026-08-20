"""频道实例领域模型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass
class ChannelInstance:
    id: str
    platform: str
    channel_id: str
    workspace_id: str
    agent_identity: str
    ambient_enabled: bool = False
    cross_channel_learning: bool = False
    policy_id: str | None = None
    # 本频道任务的默认负责人。建任务时填进 Task.owner_id，作为工具审批的
    # 第一级审批人来源（PRD §4.6 的「通知负责人」）。
    #
    # 取 CODEOWNERS 的模式：配置指定 + 可覆盖。未配置则 owner_id 为空，
    # 审批人回落到 policy.approver_ids。
    default_owner_id: str | None = None
    created_at: datetime = field(default_factory=_utcnow)
