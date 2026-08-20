"""task_checkpoints 建表

存 agent 执行到干净轮边界（历史里没有未被应答的工具调用）时的消息历史快照。
worker 崩溃后由超时巡检据此重新入队，续跑只执行剩余的工具轮次 —— 此前崩溃
一律收敛到 FAILED，已完成的工具与已花的 token 全部作废。

按 task_id 覆盖写，只留最新一份：续跑永远从最近的干净边界开始。留一串历史
要配 GC，而换不来任何东西（除了时间旅行式调试，那不在范围内）。

**单独建表而不并入 tasks**：messages 是序列化的消息历史，实测每工具轮约
1.2 KB（十轮量级 12 KB），而 tasks 被列表端点与超时巡检反复全表扫。大字段与
热扫描表放一起会拖慢后者 —— 与记忆向量那次 TOAST 的实测教训同理。

attempts 记续跑次数，超过 jobs_max_resume_attempts 即放弃、收敛到 FAILED。
它由一条 UPDATE 原子自增，且检查点的覆盖写必须保留它 —— 否则每落一个检查点
就把计数清零，反复崩溃的任务能无限续跑。

不做外键：与本项目其余表一致。终态时的清理由 TaskOrchestrator.transition 在
同一事务内显式做。

设计与 pydantic-ai 行为实测见 docs/SPEC-agent-checkpoint.md。

Revision ID: f8d3a1c26b47
Revises: e6c1f4a8b920
Create Date: 2026-08-20 14:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f8d3a1c26b47"
down_revision: str | Sequence[str] | None = "e6c1f4a8b920"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "task_checkpoints",
        sa.Column("task_id", sa.String(length=32), nullable=False),
        sa.Column("messages", sa.LargeBinary(), nullable=False),
        sa.Column("tokens_used", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("task_id"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("task_checkpoints")
