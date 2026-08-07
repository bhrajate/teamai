"""分层依赖静态校验。

用 AST 解析全部源码的 import，确保依赖只向下。把上一次重构的成果固化成
断言，防止后续改动悄悄退回 application → infrastructure 的向上依赖。
"""

from __future__ import annotations

import ast
import pathlib

import pytest

SRC = pathlib.Path(__file__).resolve().parents[2] / "src" / "teamai"

# 各层允许依赖的层集合。util 为叶子（零内部依赖），config 仅被读取。
ALLOWED: dict[str, set[str]] = {
    # 包根 src/teamai/__init__.py，当前为空；登记以便日后加了 import 也受校验
    "__init__": set(),
    "domain": {"domain", "util"},
    "util": {"util"},
    "config": {"config"},
    "tools": {"domain", "tools", "util", "config"},
    "agent": {"domain", "tools", "agent", "util", "config"},
    "application": {"domain", "application", "agent", "tools", "util", "config"},
    "infrastructure": {"domain", "infrastructure", "util", "config"},
    "container": {"domain", "application", "agent", "tools", "infrastructure", "util", "config", "container"},
    "adapters": {
        "domain", "application", "agent", "tools", "infrastructure",
        "adapters", "util", "config", "container",
    },
    "main": {
        "domain", "application", "agent", "tools", "infrastructure",
        "adapters", "util", "config", "container", "main",
    },
}


def _layer(rel: pathlib.Path) -> str:
    parts = rel.parts
    return parts[0] if len(parts) > 1 else parts[0].removesuffix(".py")


def _internal_imports(path: pathlib.Path) -> list[tuple[int, str]]:
    """返回 [(行号, 被导入的 teamai 子模块全名)]。"""
    out: list[tuple[int, str]] = []
    tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("teamai."):
            out.append((node.lineno, node.module))
        elif isinstance(node, ast.Import):
            for a in node.names:
                if a.name.startswith("teamai."):
                    out.append((node.lineno, a.name))
    return out


def _py_files() -> list[pathlib.Path]:
    return sorted(SRC.rglob("*.py"))


def test_源码目录存在() -> None:
    assert SRC.is_dir(), f"未找到源码目录: {SRC}"
    assert _py_files(), "源码目录为空"


def test_无越层依赖() -> None:
    violations: list[str] = []
    for path in _py_files():
        rel = path.relative_to(SRC)
        src_layer = _layer(rel)
        allowed = ALLOWED.get(src_layer)
        assert allowed is not None, f"{rel}: 未在 ALLOWED 中登记的层 {src_layer!r}"
        for lineno, mod in _internal_imports(path):
            tgt = mod.split(".")[1]
            if tgt not in allowed:
                violations.append(f"{rel}:{lineno} {src_layer} -> {tgt} ({mod})")
    assert not violations, "发现越层依赖:\n  " + "\n  ".join(violations)


def test_application_不依赖_infrastructure() -> None:
    """本次重构的核心目标，单列一条便于回归定位。"""
    bad: list[str] = []
    for path in sorted((SRC / "application").rglob("*.py")):
        for lineno, mod in _internal_imports(path):
            if mod.startswith("teamai.infrastructure"):
                bad.append(f"{path.relative_to(SRC)}:{lineno} {mod}")
    assert not bad, (
        "application 层不得依赖 infrastructure（持久化与队列须走 domain 抽象）:\n  "
        + "\n  ".join(bad)
    )


def test_domain_只依赖自身与_util() -> None:
    bad: list[str] = []
    for path in sorted((SRC / "domain").rglob("*.py")):
        for lineno, mod in _internal_imports(path):
            tgt = mod.split(".")[1]
            if tgt not in {"domain", "util"}:
                bad.append(f"{path.relative_to(SRC)}:{lineno} -> {tgt}")
    assert not bad, "domain 层须保持零外向依赖:\n  " + "\n  ".join(bad)


def test_domain_不导入三方库() -> None:
    """domain 应只用标准库，避免领域模型被外部框架污染。"""
    allow = {"abc", "dataclasses", "datetime", "enum", "typing", "__future__", "collections"}
    bad: list[str] = []
    for path in sorted((SRC / "domain").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for n in names:
                root = n.split(".")[0]
                if root not in allow and root != "teamai":
                    bad.append(f"{path.relative_to(SRC)}:{node.lineno} {n}")
    assert not bad, "domain 层引入了三方库:\n  " + "\n  ".join(bad)


@pytest.mark.parametrize(
    ("impl", "abstraction"),
    [
        ("teamai.infrastructure.queue:RedisTaskQueue", "teamai.domain.ports:TaskQueue"),
        ("teamai.infrastructure.repositories:SQLTaskRepository", "teamai.domain.repositories:TaskRepository"),
        ("teamai.infrastructure.repositories:SQLAuditRepository", "teamai.domain.repositories:AuditRepository"),
    ],
)
def test_实现已注册为抽象子类(impl: str, abstraction: str) -> None:
    import importlib

    def load(spec: str) -> type:
        mod, name = spec.split(":")
        return getattr(importlib.import_module(mod), name)

    assert issubclass(load(impl), load(abstraction))
