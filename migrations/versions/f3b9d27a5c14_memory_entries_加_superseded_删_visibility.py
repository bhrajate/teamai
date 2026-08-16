"""memory_entries 加 superseded_by / superseded_at，删 visibility

## 加 superseded 两列

蒸馏此前只会追加：同一事实在多个窗口被提到就存多条，而「超时 3 秒」改成
「超时 5 秒」后两条并列共存，检索按语义相似度取 top_k 时两条相似度几乎一样，
模型看到互相矛盾的上下文且没有任何信号能判断哪条是现行的。

现在蒸馏会先取该频道近似记忆作候选、由模型判定 ADD / UPDATE / NOOP，UPDATE
时新写一条并给旧条目打 superseded_by。检索一律带 `superseded_by IS NULL`。

不物理删除旧条目：与 mem0 / Zep 的取舍一致（两者都是 mark invalid rather than
physically removing）。「矛盾」的判断来自模型、可能错，而删除不可逆；留着还能
回答「这条事实之前是什么、被什么取代」。

只加一维时间而非 Zep 的双时间轴（arXiv:2501.13956 §2.1 分开记 t_valid /
t_invalid 与 t'_created / t'_expired）：本项目蒸馏近实时（窗口满 20 条或静置
600s 即触发），created_at 与事实实际成立时间偏差在分钟级，双时间轴收益接近零。

两列都可空，无需回填 —— 既有行全部是未被取代的现行事实，NULL 正是要表达的。

## 删 visibility

该列自建立起就是死字段：没有任何调用方传过非默认值，全部行都是 'channel'；
检索侧（MemoryService.query_for_context）无论走向量还是时间倒序回落，都不看
这一列。「私密内容不进记忆」这个承诺实际由 router 在进蒸馏窗口**之前**丢弃
单聊消息来兜（PRIVATE_CHANNEL_TYPES 判定），与本列无关。

删而不是留着标注废弃：跨频道记忆检索是接下来要做的事，而那正是要拿可见性做
判断的阶段。留着一个「看起来在做权限控制、实际恒为默认值」的列，届时写
`WHERE visibility != 'private'` 会静默匹配零行并被当成已获得保护 —— 删掉之后
同样的写法在开发期就报错。单条记忆的可见性判定将由 ChannelInstance 的来源
可见性承载：那是客观事实（频道本身是公开还是私密），而非写入时的判断。

删除不丢信息：全部行都是默认值。

Revision ID: f3b9d27a5c14
Revises: e5a71c9d3b28
Create Date: 2026-08-16 10:30:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f3b9d27a5c14"
down_revision: str | Sequence[str] | None = "e5a71c9d3b28"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_VISIBILITY_ENUM = "visibility"
_VISIBILITY_VALUES = ("channel", "private")


def upgrade() -> None:
    """Upgrade schema."""
    # superseded_by 不设外键：被取代的条目可能因人工删除而消失
    # （MemoryService.delete 是物理删除），外键会让那次删除失败或级联清掉指针 ——
    # 前者阻断正常运维，后者丢掉「这条被取代过」这个事实。读取方只用它判
    # NULL / 非 NULL，解引用失败不影响正确性。
    op.add_column(
        "memory_entries",
        sa.Column("superseded_by", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "memory_entries",
        sa.Column("superseded_at", sa.DateTime(timezone=True), nullable=True),
    )
    # 建索引：检索路径一律带 `superseded_by IS NULL`，这是每次查询都要过的条件
    op.create_index(
        "ix_memory_entries_superseded_by",
        "memory_entries",
        ["superseded_by"],
    )

    op.drop_column("memory_entries", "visibility")
    # 枚举类型在 Postgres 里是独立对象，drop_column 不会带走它 ——
    # 不显式删掉，将来若有同名类型会撞 DuplicateObject（与 d2e8b41f7c96
    # 处理 memorysource 时同一个坑）。
    sa.Enum(name=_VISIBILITY_ENUM).drop(op.get_bind(), checkfirst=True)


def downgrade() -> None:
    """Downgrade schema."""
    # 先重建枚举类型再加列：Postgres 下 add_column 不会自动建枚举
    enum = sa.Enum(*_VISIBILITY_VALUES, name=_VISIBILITY_ENUM)
    enum.create(op.get_bind(), checkfirst=True)
    op.add_column(
        "memory_entries",
        sa.Column(
            "visibility",
            sa.Enum(*_VISIBILITY_VALUES, name=_VISIBILITY_ENUM, create_type=False),
            nullable=True,
        ),
    )
    # 回填为 channel：升级前全部行都是这个值，回滚后保持一致
    op.execute("UPDATE memory_entries SET visibility = 'channel' WHERE visibility IS NULL")

    op.drop_index("ix_memory_entries_superseded_by", table_name="memory_entries")
    op.drop_column("memory_entries", "superseded_at")
    op.drop_column("memory_entries", "superseded_by")
