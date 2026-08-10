"""budget_quotas 加 period_started_at

供周期重置巡检判断配额是否该翻页。不复用 updated_at：后者每次消费都会刷新，
拿它当周期起点会让「一直在用的频道」永远不满足重置条件。

已有行回填为 updated_at —— 那是关于该配额最后一次活动时间的唯一线索，比回填
建表时间或当前时间更接近真实周期起点。回填后首次巡检可能立即触发一次重置
（若 updated_at 已超过一个周期），这正是期望行为：那些配额本就该重置了。

Revision ID: a1c4e8f2b7d3
Revises: 792b24214125
Create Date: 2026-08-10 16:20:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a1c4e8f2b7d3"
down_revision: str | Sequence[str] | None = "792b24214125"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    # 分三步而非直接加 NOT NULL 列：已有行需要回填，而 NOT NULL 无默认值时
    # 加列会直接失败。先可空 → 回填 → 收紧约束。
    op.add_column(
        "budget_quotas",
        sa.Column("period_started_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.execute("UPDATE budget_quotas SET period_started_at = updated_at")
    op.alter_column("budget_quotas", "period_started_at", nullable=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("budget_quotas", "period_started_at")
