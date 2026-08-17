"""合表：偏好并入 memory_entries（type='PREFERENCE'），删 preferences 表

## 为什么删表

preferences 表自建立起就是死结构：

- `MemoryService.set_preference` 没有任何业务调用方（无 Admin 端点、无前端 UI，
  router / agent 均不碰），`query_for_context` 读 `repo.list_preferences()`
  恒空 —— 「偏好不参与向量检索、一律全带上」的设计承诺（application/memory.py
  的文档）实际带的是 0 条。
- 蒸馏产出的 PREFERENCE 类型却走 memory_entries **参与向量 top_k 竞争**，
  与「偏好不能被相似度筛掉」的语义相反。

合表后偏好作为 memory_entries 里 `type='PREFERENCE'` 的行统一存储，检索按类型
分层：语义命中段排除偏好（偏好不与事实抢 top_k 名额），偏好段由
`query_for_context` / `find_similar` 显式全量取。偏好同样享受 supersede / edit /
delete / source 溯源治理，且可被蒸馏相互取代（此前两表绝缘）。

## upgrade：转存 + drop

preferences 若有存量行（死结构下正常应无），先 INSERT 进 memory_entries：
保留原 `pref_<ULID>` id（与 `mem_<ULID>` 同为 30 字符、顶满 String(32)，
保留原 id 让 downgrade→upgrade 循环可幂等）；type 用枚举字面量 'PREFERENCE'；
source 记为 MANUAL（那是人写下的）；created_at 沿用。然后 drop 表。
preferences 无独立 enum 类型，无需像 visibility 那样额外 drop 类型对象。

## downgrade：重建 + 尽力回填

重建 preferences 表与索引，从 memory_entries 回填**现行**偏好（type='PREFERENCE'
且 `superseded_by IS NULL`，被取代的旧偏好不应复活）。preferences.user_id 是
NOT NULL 而蒸馏偏好的 source_user_id 可为 NULL，故 COALESCE 占位。回填注定
非无损：source 与 superseded_* 字段带不回，这是「尽力回填」。

Revision ID: 2120bce9e0f3
Revises: f3b9d27a5c14
Create Date: 2026-08-18 10:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "2120bce9e0f3"
down_revision: str | Sequence[str] | None = "f3b9d27a5c14"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    bind = op.get_bind()
    # 死结构，正常应无存量；有则先转存再 drop，避免丢数据。
    # 幂等：downgrade 回填会保留原 id 从 memory_entries 复制回 preferences，
    # 二次 upgrade 时若 memory_entries 里已有同 id 行（正是回填来源），跳过转存，
    # 避免主键冲突。新写入的偏好（id 不在 memory_entries）照常转存。
    existing = bind.execute(sa.text("SELECT 1 FROM preferences LIMIT 1")).scalar()
    if existing is not None:
        op.execute(
            sa.text(
                """
                INSERT INTO memory_entries
                    (id, channel_instance_id, content, type, source_user_id, source, created_at)
                SELECT
                    id, channel_instance_id, preference, 'PREFERENCE', user_id, 'MANUAL',
                    COALESCE(created_at, now())
                FROM preferences
                WHERE NOT EXISTS (
                    SELECT 1 FROM memory_entries WHERE memory_entries.id = preferences.id
                )
                """
            )
        )
    op.drop_table("preferences")


def downgrade() -> None:
    """Downgrade schema."""
    op.create_table(
        "preferences",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("channel_instance_id", sa.String(length=32), nullable=False),
        sa.Column("user_id", sa.String(length=64), nullable=False),
        sa.Column("preference", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_preferences_channel_instance_id"),
        "preferences",
        ["channel_instance_id"],
        unique=False,
    )
    # 尽力回填现行偏好：source_user_id 可为 NULL，用占位顶 NOT NULL
    op.execute(
        sa.text(
            """
            INSERT INTO preferences (id, channel_instance_id, user_id, preference, created_at)
            SELECT
                id,
                channel_instance_id,
                COALESCE(source_user_id, 'unknown'),
                content,
                created_at
            FROM memory_entries
            WHERE type = 'PREFERENCE' AND superseded_by IS NULL
            """
        )
    )