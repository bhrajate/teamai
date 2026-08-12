"""固化「SQL 仓储的写方法必须 commit」这条约束。

背景：容器持有单个共享 AsyncSession。写方法若只 add/merge/delete 而不提交，
改动只留在该 session 的 identity map 里 —— 同进程读得到，别的进程读不到。
项目原先 12 个写方法全都没提交，同步链路（web 进程内读写同一 session）
一直掩盖着它；长任务链路一打通就暴露：worker 是另一个进程、另一个 session，
出队后一律报「任务不存在」。

用 AST 静态检查而非连库跑：与 test_orm_registry 同一思路，且无外部依赖，
CI 里必然执行。新增写方法忘了 commit 时，这里会红。
"""

from __future__ import annotations

import ast
import pathlib

import pytest

REPO_DIR = (
    pathlib.Path(__file__).resolve().parents[2]
    / "src"
    / "teamai"
    / "infrastructure"
    / "repositories"
)

# session 上表示「改了数据」的方法
_MUTATING_CALLS = {"add", "merge", "delete", "add_all", "execute"}
# execute 只有跑 DML 才算写，这里不做 SQL 解析，故单独列出已知的只读用法前缀
_READONLY_EXECUTE_HINT = "select"


def _repo_modules() -> list[pathlib.Path]:
    return sorted(p for p in REPO_DIR.glob("*.py") if p.stem != "__init__")


def _self_session_attr(node: ast.AST) -> str | None:
    """匹配 `self._session.<name>` 形态的调用，返回 <name>。"""
    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
        return None
    inner = node.func.value
    if (
        isinstance(inner, ast.Attribute)
        and inner.attr == "_session"
        and isinstance(inner.value, ast.Name)
        and inner.value.id == "self"
    ):
        return node.func.attr
    return None


def _builds_dml(fn: ast.AsyncFunctionDef) -> bool:
    """函数体里是否构造了 DML 语句（`delete()` / `update()` / `insert()`）。

    扫整个函数体而非只看 `execute()` 的实参：语句通常先赋给变量再执行
    （`stmt = delete(X).where(...)` 然后 `await self._session.execute(stmt)`），
    只匹配内联实参会漏掉这种最常见的写法。

    不做 SQL 解析，只认这三个构造器的名字 —— select 走读路径，不算写。
    """
    for node in ast.walk(fn):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in {"delete", "update", "insert"}
        ):
            return True
    return False


def _mutates(fn: ast.AsyncFunctionDef) -> bool:
    """方法是否改动了数据。

    两种形态：调 session.add/merge/delete，或直接改已加载模型的属性
    （set_active 就是 `m.active = active`，不经 session 方法）。
    """
    for node in ast.walk(fn):
        name = _self_session_attr(node)
        if name in _MUTATING_CALLS and name != "execute":
            return True
        # `session.execute(delete(...))` —— 批量 DML 不经 add/merge/delete，
        # 但确实改数据且必须 commit。
        if name == "execute" and _builds_dml(fn):
            return True
        # `m.active = active` —— 对非 self 对象的属性赋值视为改模型
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if (
                    isinstance(target, ast.Attribute)
                    and isinstance(target.value, ast.Name)
                    and target.value.id != "self"
                ):
                    return True
    return False


def _commits(fn: ast.AsyncFunctionDef) -> bool:
    return any(_self_session_attr(node) == "commit" for node in ast.walk(fn))


def _write_methods() -> list[tuple[str, str, bool]]:
    """返回 (模块名, 方法名, 是否 commit)，仅含改动数据的方法。"""
    out: list[tuple[str, str, bool]] = []
    for path in _repo_modules():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for cls in (n for n in tree.body if isinstance(n, ast.ClassDef)):
            for fn in (n for n in cls.body if isinstance(n, ast.AsyncFunctionDef)):
                if _mutates(fn):
                    out.append((path.stem, fn.name, _commits(fn)))
    return out


def test_扫描到了写方法() -> None:
    """自检：解析器若失灵会一个都扫不到，那后面的断言就成了空转。"""
    methods = _write_methods()
    assert len(methods) >= 12, f"写方法数量异常，解析可能失灵: {methods}"


@pytest.mark.parametrize("module,method,has_commit", _write_methods(), ids=lambda v: str(v))
def test_写方法必须提交(module: str, method: str, has_commit: bool) -> None:
    assert has_commit, (
        f"{module}.{method} 改了数据但没 commit —— 共享 session 下这条改动"
        f"对别的进程不可见，worker 会读不到。"
    )


def test_只读方法不提交() -> None:
    """反向约束：查询方法不该 commit，否则白跑一次事务往返。"""
    offenders: list[str] = []
    for path in _repo_modules():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for cls in (n for n in tree.body if isinstance(n, ast.ClassDef)):
            for fn in (n for n in cls.body if isinstance(n, ast.AsyncFunctionDef)):
                if not _mutates(fn) and _commits(fn):
                    offenders.append(f"{path.stem}.{fn.name}")
    assert not offenders, f"只读方法不该 commit: {offenders}"
