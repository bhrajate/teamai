"""工具审批（HITL）扩列

危险工具（改外部系统的那些）执行前中断，等指定的人批准。核心是四眼原则：
发起人不得批准自己的动作，关键动作可要求两个不同的人各批一次。

三张表各扩列，不新建表：

- `permission_policies`：审批配置。approval_required_tools 是「工具名 → 需要几个
  批准」的 JSON 对象；approver_ids 是频道级审批人。两者为空且任务无 owner_id 时，
  需审批的工具**拒绝执行**而非放宽 —— 与 allowed_tools 的语义一致。
- `channel_instances`：default_owner_id，建任务时填进 tasks.owner_id，作为审批人
  的第一级来源（兑现 PRD §4.6 的「通知负责人」；该字段此前是空壳，零赋值点）。
- `task_checkpoints`：pending_approval 存待批的工具调用。与检查点同表是因为两者
  都是同一任务的执行期状态、主键都是 task_id、终态时一起清。

不新增 AuditAction 成员：审批事件复用 POLICY_CHANGE + detail.event
（approval_required / granted / denied / timeout / rejected_self）。action 在
Postgres 上是原生枚举，加成员必须配 ALTER TYPE 迁移，漏了会让已升级的库在写
审计时抛 InvalidTextRepresentationError —— 见 tests/unit/test_enum_migrations.py。

设计与业界实践对照见 docs/SPEC-hitl-approval.md。

Revision ID: a2f7e91b34c5
Revises: f8d3a1c26b47
Create Date: 2026-08-20 16:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a2f7e91b34c5"
down_revision: str | Sequence[str] | None = "f8d3a1c26b47"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "permission_policies",
        sa.Column("approval_required_tools", sa.Text(), nullable=False, server_default="{}"),
    )
    op.add_column(
        "permission_policies",
        sa.Column("approver_ids", sa.Text(), nullable=False, server_default="[]"),
    )
    op.add_column(
        "channel_instances",
        sa.Column("default_owner_id", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "task_checkpoints",
        sa.Column("pending_approval", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("task_checkpoints", "pending_approval")
    op.drop_column("channel_instances", "default_owner_id")
    op.drop_column("permission_policies", "approver_ids")
    op.drop_column("permission_policies", "approval_required_tools")
