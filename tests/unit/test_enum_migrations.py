"""固化「Python 枚举加了值，必须同时迁移 Postgres 枚举类型」这条约束。

背景（真实故障）：`AuditAction` 加 `MEMORY_DISTILL` 时只改了 Python 枚举，没有
迁移 `auditaction` 类型。于是记忆蒸馏在**任何已存在的库上**都失败 —— 写审计时
asyncpg 抛 `InvalidTextRepresentationError: invalid input value for enum
auditaction: "MEMORY_DISTILL"`，异常冒泡到按频道的兜底，整个频道被记成蒸馏失败。

为什么现有测试全都抓不到它：

- application 层的测试用内存替身，根本不碰数据库；
- 仓储层的真 SQL 测试跑在 SQLite 上，那里 `sa.Enum` 落成 VARCHAR + CHECK，
  且 CHECK 是按当前 Python 定义生成的，插什么都过；
- `init_db()` 的 `create_all` 在新库上会按当前定义建出完整枚举，所以本地开发
  和 CI 的新库都正常 —— 只有升级过的库会炸。

这里用静态检查兜住：枚举值只要出现在某个迁移文件里就算已交代。不连库、无外部
依赖，CI 必然执行。误判方向是安全的（漏改必红，改了不会误红）。
"""

from __future__ import annotations

import pathlib

import pytest
import sqlalchemy as sa

import teamai.infrastructure.orm  # noqa: F401  触发表注册
from teamai.infrastructure.db import Base

MIGRATIONS = pathlib.Path(__file__).resolve().parents[2] / "migrations" / "versions"


def _migration_text() -> str:
    """全部迁移文件拼成一段文本。

    不解析 AST：枚举值可能出现在 sa.Enum(...) 的实参、`ALTER TYPE ... ADD VALUE`
    的字符串里、或回填 SQL 的字面量里 —— 形态太多，按文本找反而更稳。
    """
    return "\n".join(p.read_text(encoding="utf-8") for p in sorted(MIGRATIONS.glob("*.py")))


def _persisted_enums() -> list[tuple[str, str, tuple[str, ...]]]:
    """返回 [(表名.列名, 枚举类名, 全部取值)]，仅含真的落库的枚举列。"""
    out: list[tuple[str, str, tuple[str, ...]]] = []
    for table in Base.metadata.sorted_tables:
        for col in table.columns:
            if isinstance(col.type, sa.Enum):
                cls = col.type.enum_class
                name = cls.__name__ if cls is not None else col.type.name or "?"
                out.append((f"{table.name}.{col.name}", name, tuple(sorted(col.type.enums))))
    return out


def test_扫描到了枚举列() -> None:
    """自检：解析失灵会一个都扫不到，那下面的断言就成了空转。"""
    found = _persisted_enums()
    assert len(found) >= 8, f"落库的枚举列数量异常，解析可能失灵: {found}"


@pytest.mark.parametrize(
    ("column", "enum_name", "values"),
    _persisted_enums(),
    ids=lambda v: str(v) if not isinstance(v, tuple) else "-".join(v),
)
def test_枚举取值都在迁移里交代过(column: str, enum_name: str, values: tuple[str, ...]) -> None:
    text = _migration_text()
    missing = [v for v in values if v not in text]
    assert not missing, (
        f"{column}（{enum_name}）的取值 {missing} 只存在于 Python 枚举，"
        f"没有任何迁移提到它们。\n"
        f"新库经 create_all 会正常，但**已升级过的库上写入这个值会直接报错**"
        f"（invalid input value for enum）。\n"
        f"补一条迁移：ALTER TYPE {enum_name.lower()} ADD VALUE IF NOT EXISTS '<值>'"
    )


def test_迁移目录非空() -> None:
    assert list(MIGRATIONS.glob("*.py")), f"未找到迁移文件: {MIGRATIONS}"
