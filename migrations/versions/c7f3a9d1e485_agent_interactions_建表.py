"""agent_interactions 建表

存机器人自己的交互记录：实际组装出的提示词、模型响应、分拆的 in/out token、
以及本次引用了哪些记忆。补的是审计的一个缺口 —— audit_logs 只记动作枚举
（九个 AuditAction）加一个小字典，还原不出「模型当时看到了什么」，于是回答
错了、越权调了工具、token 烧超了都无法复现。

与 audit_logs 分表而非加列：一张记动作流水（永久留存、字段窄、可按枚举统计），
一张记内容快照（含全文、按 interactions_retention_days 清理）。合成一张的话，
要么审计被大字段拖胖，要么内容被塞进 detail 的 JSON 里而没法按字段查询与统计成本。

三个字段值得单独说明：
- model_id 存实际生效的模型而非配置档位。light 档走 FallbackModel(primary →
  fallback)，主模型失败时真正跑的是备用模型，两者单价可能差数倍。
- tokens_in / tokens_out 分开。多数供应商输入输出单价差 3-5 倍，只记 total
  没法做成本归因。
- context_refs 存引用（memory_entry.id 列表）而非内容快照。管理员删掉某条记忆后
  审计链仍指出「当时引用过它」，但库里不留第二份副本 —— 否则「删除记忆」是假的。

暂不分区：默认保留期 90 天下单表规模有限，分区的迁移与运维复杂度此时不划算。
等真实数据量证明需要再按 created_at 做 RANGE 分区。

Revision ID: c7f3a9d1e485
Revises: a1c4e8f2b7d3
Create Date: 2026-08-11 16:40:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c7f3a9d1e485"
down_revision: str | Sequence[str] | None = "a1c4e8f2b7d3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "agent_interactions",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("task_id", sa.String(length=32), nullable=False),
        sa.Column("channel_instance_id", sa.String(length=32), nullable=False),
        # thread_ref 取值由平台决定：slack 是 thread_ts（1700000000.000100），
        # feishu 是 message_id（om_ + 32hex）。128 留足余量。
        sa.Column("thread_ref", sa.String(length=128), nullable=False),
        # 飞书 user_id 是 ou_ + 32hex，与其他表一致取 64
        sa.Column("requester_id", sa.String(length=64), nullable=True),
        sa.Column("user_prompt", sa.Text(), nullable=False),
        sa.Column("system_prompt", sa.Text(), nullable=False),
        sa.Column("context_refs", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("model_level", sa.String(length=16), nullable=False),
        sa.Column("model_id", sa.String(length=128), nullable=False, server_default=""),
        sa.Column("response", sa.Text(), nullable=False, server_default=""),
        sa.Column("tokens_in", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("tokens_out", sa.Integer(), nullable=False, server_default="0"),
        # 与 ORM 的 Enum(InteractionResult) 对应。显式命名约束，否则各方言
        # 自动生成的名字不同，将来要改这个枚举时 downgrade 找不到它。
        sa.Column(
            "result",
            sa.Enum("DONE", "PAUSED", "FAILED", name="interactionresult"),
            nullable=False,
        ),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_agent_interactions_task_id", "agent_interactions", ["task_id"])
    op.create_index(
        "ix_agent_interactions_channel_instance_id",
        "agent_interactions",
        ["channel_instance_id"],
    )
    op.create_index("ix_agent_interactions_created_at", "agent_interactions", ["created_at"])
    # 复合索引单独建：控制台「某频道最近 50 条」与保留期清理都吃它。
    # 只有单列索引时，前者要先取该频道全部行再排序。
    op.create_index(
        "ix_agent_interactions_channel_created",
        "agent_interactions",
        ["channel_instance_id", "created_at"],
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_agent_interactions_channel_created", table_name="agent_interactions")
    op.drop_index("ix_agent_interactions_created_at", table_name="agent_interactions")
    op.drop_index(
        "ix_agent_interactions_channel_instance_id", table_name="agent_interactions"
    )
    op.drop_index("ix_agent_interactions_task_id", table_name="agent_interactions")
    op.drop_table("agent_interactions")
    # 枚举类型在 Postgres 里是独立对象，drop_table 不会带走它 ——
    # 不显式删掉，重跑 upgrade 会撞 DuplicateObject。
    sa.Enum(name="interactionresult").drop(op.get_bind(), checkfirst=True)
