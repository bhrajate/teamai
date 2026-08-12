"""memory_entries 加 source（产生方式）

区分「这条记忆是谁写下的」：DISTILLED（模型从对话窗口蒸馏）/ MANUAL（人经
Admin API 写入）/ EDITED（原为蒸馏产出、后被人工修正）。

为什么不用已有的 source_user_id 表达：那一列答的是「哪个用户的话变成了这条」，
而蒸馏产出与管理台人工写入的 source_user_id **都是 NULL** —— 控制台里两者
显示成同一个「系统」，完全不可区分。而这张表的内容直接影响机器人的回答，
「这句话是谁写的」是出问题时第一个要问的，只靠审计流水回溯太绕。

已有行回填为 DISTILLED：改造前 router 把每条非 @ 消息直接塞进这张表（见
docs/Design-conversation-context.md §1），那些行确实都是系统自动写入的，
不是人手写的。回填成 MANUAL 会把「系统攒的聊天碎片」错标成「人工录入的知识」，
恰好反了。

Revision ID: d2e8b41f7c96
Revises: c7f3a9d1e485
Create Date: 2026-08-11 18:05:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d2e8b41f7c96"
down_revision: str | Sequence[str] | None = "c7f3a9d1e485"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ENUM_NAME = "memorysource"
_VALUES = ("DISTILLED", "MANUAL", "EDITED")


def upgrade() -> None:
    """Upgrade schema."""
    # 分三步而非直接加 NOT NULL 列：已有行需要回填，而 NOT NULL 无默认值时
    # 加列会直接失败。先可空 → 回填 → 收紧约束。与 budget_quotas 加
    # period_started_at 那次同一套做法。
    #
    # create_type=False + 显式 create()：Postgres 下 add_column 不会自动建
    # 枚举类型，而 sa.Enum 在别处（ORM 的 Enum(MemorySource)）也会引用它。
    enum = sa.Enum(*_VALUES, name=_ENUM_NAME)
    enum.create(op.get_bind(), checkfirst=True)

    op.add_column(
        "memory_entries",
        sa.Column("source", sa.Enum(*_VALUES, name=_ENUM_NAME, create_type=False), nullable=True),
    )
    op.execute("UPDATE memory_entries SET source = 'DISTILLED' WHERE source IS NULL")
    op.alter_column("memory_entries", "source", nullable=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("memory_entries", "source")
    # 枚举类型在 Postgres 里是独立对象，drop_column 不会带走它 ——
    # 不显式删掉，重跑 upgrade 会撞 DuplicateObject。
    sa.Enum(name=_ENUM_NAME).drop(op.get_bind(), checkfirst=True)
