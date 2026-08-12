"""auditaction 枚举补 MEMORY_DISTILL 与 MEMORY_EDIT

修一个已经进了主干的缺陷：`AuditAction` 上一次加 `MEMORY_DISTILL` 时只改了
Python 枚举，没有迁移 Postgres 的 `auditaction` 类型。于是**记忆蒸馏在任何已
存在的库上都是失败的** —— 写审计时 asyncpg 抛
`InvalidTextRepresentationError: invalid input value for enum auditaction:
"MEMORY_DISTILL"`，异常冒泡到 MemoryDistiller 的按频道兜底，整个频道被记成蒸馏
失败。只有全新 `create_all` 出来的库不会踩到（那时枚举是按当前 Python 定义建的）。

单测抓不到这类缺陷：application 层用的是内存替身，而仓储层的真 SQL 测试跑在
SQLite 上 —— 那里 `Enum` 落成 VARCHAR + CHECK，插入未登记的值不会像 Postgres
那样报错。只有对真 Postgres 打一次请求才会暴露。

本次同时补上 `MEMORY_EDIT`（记忆编辑）。

⚠️ `ALTER TYPE ... ADD VALUE` 在 PG 12+ 允许出现在事务里（本项目跑 16），但
**同一事务内不能使用刚加的值** —— 所以这里只加值、不做任何回填。

`IF NOT EXISTS` 让本迁移在「已经 create_all 出完整枚举」的库上也能跑过，
不然新库与旧库需要两条不同的升级路径。

Revision ID: e5a71c9d3b28
Revises: d2e8b41f7c96
Create Date: 2026-08-11 18:40:00.000000

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e5a71c9d3b28"
down_revision: str | Sequence[str] | None = "d2e8b41f7c96"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_NEW_VALUES = ("MEMORY_DISTILL", "MEMORY_EDIT")


def upgrade() -> None:
    """Upgrade schema."""
    for value in _NEW_VALUES:
        op.execute(f"ALTER TYPE auditaction ADD VALUE IF NOT EXISTS '{value}'")


def downgrade() -> None:
    """Downgrade schema。

    Postgres 没有 `ALTER TYPE ... DROP VALUE`。要真正移除得重建整个类型：
    建新类型 → 改所有引用列 → 删旧类型，而 audit_logs 是只追加的审计表，
    期间还要处理已经写进去的 MEMORY_DISTILL / MEMORY_EDIT 行（删掉？改成别的
    动作？两者都是在篡改审计）。

    代价与收益不成比例，故这条迁移单向：多两个用不到的枚举值是无害的，
    而为了「能回退」去改审计数据是有害的。
    """
    pass
