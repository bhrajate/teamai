"""memory_outbox 建表；memory_entries 加 embedded_hash

## 为什么加这张表

改造前记忆写入与向量写入是两次独立提交（`MemoryService.store` 先
`_repo.store()` 再 `_embed_if_available()`），中间崩溃就得到一条永远没有向量的
记忆 —— 而它不会被任何机制发现，因为项目没有对账。现在「该重算向量」这个意图
与记忆行同事务落库，由 worker 里的常驻 projector 消费。

完整设计见 `docs/plan-memory-outbox.md`；本迁移只对应 §5.3 的数据模型。

## embedded_hash 为什么必须与 embedding_ref 并存

两列答的是不同问题：`embedding_ref` 答「向量存不存在」，`embedded_hash` 答
「向量是按哪份内容建的」。对账谓词同时要这两个 —— 只看 ref 判不出「编辑过但
向量没重算」，只看 hash 判不出「向量丢了」。

存量行 embedded_hash 留 NULL 且**不回填**：改造前的向量来自双写路径，无从追溯
当时用的是哪份内容。对账会把它们全部判为「需重算」，重新 embed 一遍即可
（单频道几百条，成本可忽略），比写一个猜测原内容的回填脚本可靠。

## downgrade

drop 表与列。`outboxop` 枚举类型要显式 drop —— Postgres 里枚举是独立对象，
drop 表不会带走它，不显式删则 downgrade → upgrade 往返会撞 DuplicateObject。
同 `d2e8b41f7c96` 与 `f3b9d27a5c14` 对 visibility 的处理。

⚠️ downgrade 会丢掉队列里未处理的投影意图。那不影响正确性：记忆行本身还在，
回到旧代码后向量由旧的同步双写路径负责；若再 upgrade 回来，对账会把缺向量与
hash 不符的行全部重新入队。

Revision ID: b4e6c2a91d78
Revises: 2120bce9e0f3
Create Date: 2026-08-18 16:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b4e6c2a91d78"
down_revision: str | None = "2120bce9e0f3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# 枚举取值与 domain/models/outbox.py 的 OutboxOp 逐字对应。
# tests/unit/test_enum_migrations.py 会核对两者一致 —— 那条测试守的是
# 「已升级过的库上写入新枚举值直接报错」这个坑。
_OUTBOX_OPS = ("UPSERT", "DELETE")


def upgrade() -> None:
    op.create_table(
        "memory_outbox",
        sa.Column("id", sa.String(length=32), nullable=False),
        # 不做外键：记忆被物理删除后这条仍要被处理（projector 回读为空即触发
        # 删向量）。加外键会让删除失败或级联清掉这条，前者阻断运维，后者留下
        # 孤儿向量。与 memory_entries.superseded_by 同类取舍。
        sa.Column("entry_id", sa.String(length=32), nullable=False),
        sa.Column("op", sa.Enum(*_OUTBOX_OPS, name="outboxop"), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claimed_by", sa.String(length=64), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("failed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    # 抢占查询按 failed_at / next_attempt_at 过滤、按 created_at 排序，
    # 三列各建索引。entry_id 供排查「这条记忆的投影历史」用。
    op.create_index(op.f("ix_memory_outbox_entry_id"), "memory_outbox", ["entry_id"])
    op.create_index(
        op.f("ix_memory_outbox_next_attempt_at"), "memory_outbox", ["next_attempt_at"]
    )
    op.create_index(op.f("ix_memory_outbox_failed_at"), "memory_outbox", ["failed_at"])
    op.create_index(op.f("ix_memory_outbox_created_at"), "memory_outbox", ["created_at"])

    op.add_column(
        "memory_entries",
        sa.Column("embedded_hash", sa.String(length=32), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("memory_entries", "embedded_hash")

    op.drop_index(op.f("ix_memory_outbox_created_at"), table_name="memory_outbox")
    op.drop_index(op.f("ix_memory_outbox_failed_at"), table_name="memory_outbox")
    op.drop_index(op.f("ix_memory_outbox_next_attempt_at"), table_name="memory_outbox")
    op.drop_index(op.f("ix_memory_outbox_entry_id"), table_name="memory_outbox")
    op.drop_table("memory_outbox")

    # 枚举类型是独立对象，drop 表不会带走它。checkfirst 是为了在 SQLite 等
    # 无枚举类型的方言上安全跳过。
    sa.Enum(name="outboxop").drop(op.get_bind(), checkfirst=True)
