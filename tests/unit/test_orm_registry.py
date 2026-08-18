"""orm 包表注册完整性校验。

orm.py 拆成 orm/ 后多了一个隐式依赖：SQLAlchemy 只在类定义被执行时才把表
注册进 Base.metadata，而 init_db() 依赖 Base.metadata.create_all 建表。
若新增表模块却忘了在 orm/__init__.py 里 import，该表会静默地不被创建 ——
不报错、不告警，只在运行时缺表。这里把这个约束固化成断言。
"""

from __future__ import annotations

import ast
import pathlib

ORM_DIR = pathlib.Path(__file__).resolve().parents[2] / "src" / "teamai" / "infrastructure" / "orm"

# 期望建出的全部表，新增表时同步更新
EXPECTED_TABLES = {
    "agent_interactions",
    "audit_logs",
    "budget_quotas",
    "channel_instances",
    "memory_entries",
    "memory_outbox",
    "permission_policies",
    "tag_templates",
    "tasks",
}


def _table_modules() -> list[str]:
    """orm/ 下除 __init__ 外的全部模块名。"""
    return sorted(p.stem for p in ORM_DIR.glob("*.py") if p.stem != "__init__")


def _modules_imported_by_init() -> set[str]:
    """静态解析 orm/__init__.py，取出它导入的同包子模块名。

    用 AST 而非真的 import：直接 import 子模块会把表注册上，
    反而掩盖了「__init__ 里漏了它」这个缺陷。
    """
    tree = ast.parse((ORM_DIR / "__init__.py").read_text(encoding="utf-8"))
    prefix = "teamai.infrastructure.orm."
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith(prefix):
            out.add(node.module[len(prefix) :])
        elif isinstance(node, ast.Import):
            for a in node.names:
                if a.name.startswith(prefix):
                    out.add(a.name[len(prefix) :])
    return out


def test_orm_目录非空() -> None:
    assert ORM_DIR.is_dir(), f"未找到 orm 目录: {ORM_DIR}"
    assert _table_modules(), "orm 目录下没有表模块"


def test_init_导入了全部表模块() -> None:
    missing = set(_table_modules()) - _modules_imported_by_init()
    assert not missing, (
        "orm/__init__.py 漏了这些表模块，其表不会注册到 Base.metadata、"
        "init_db() 也就不会建表:\n  " + "\n  ".join(sorted(missing))
    )


def test_导入_orm_包即注册全部表() -> None:
    """只导入包本身（不碰子模块），metadata 里就该有全部表。"""
    import teamai.infrastructure.orm  # noqa: F401  触发注册
    from teamai.infrastructure.db import Base

    assert set(Base.metadata.tables) == EXPECTED_TABLES
