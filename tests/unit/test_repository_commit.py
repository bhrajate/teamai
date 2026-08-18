"""固化「SQL 仓储的写方法**不得** commit，只 flush」这条约束。

⚠️ 本文件的不变量在 2026-08-18 被**反转**了，两段历史都要留着，否则后来者会把
它改回去：

**旧约束（已废除）**：写方法必须 commit。当时容器持有单个共享 AsyncSession，
写方法若只 add/merge/delete 而不提交，改动只留在该 session 的 identity map 里
—— 同进程读得到，别的进程读不到。项目原先 12 个写方法全都没提交，同步链路
（web 进程内读写同一 session）一直掩盖着它；长任务链路一打通就暴露：worker 是
另一个进程、另一个 session，出队后一律报「任务不存在」。

**新约束**：引入 `UnitOfWork` 后事务边界由用例层声明，仓储各自提交会把本该
原子的一组写入拆成多次提交。对 outbox 方案伤害最大 —— 「写记忆」与「记下该建
向量的意图」若分两次提交，中间崩溃就丢掉后者，而那正是 outbox 要消除的窗口。

`flush` 而非 `commit`：flush 把 SQL 发到数据库，同一 session 内后续读可见
（`MemoryService.supersede` 依赖这一点 —— 写完新条目紧接着按 id 读旧条目），
但不结束事务，故整组写入仍能一起回滚。跨进程可见性现在由用例层的 UoW 提交
保证，不再是仓储的责任。理由与完整设计见 `docs/plan-memory-outbox.md` §5.5。

用 AST 静态检查而非连库跑：与 test_orm_registry 同一思路，无外部依赖，CI 里
必然执行。新增写方法忘了 flush、或顺手写了 commit 时，这里会红。
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


def _calls(fn: ast.AsyncFunctionDef, name: str) -> bool:
    return any(_self_session_attr(node) == name for node in ast.walk(fn))


# 尚未按 UoW 改造的仓储模块。改造顺序见 docs/plan-memory-outbox.md §6 第 3 步。
# 每改完一个就从这里删掉一行 —— 这个集合是进度表，空掉即第 3 步完成。
#
# 为什么用 xfail 而不是先改完再上断言：第 3 步依赖第 2 步（session-per-request）
# 与第 4 步（服务层事务边界）先落地。在那之前去掉 commit 会让写入不落盘，
# 套件红成一片，真正的回归就藏不住了。
_PENDING_UOW = {"audit", "budget", "channel", "interaction", "policy", "tag", "task"}


def _write_methods() -> list[tuple[str, str, bool, bool]]:
    """返回 (模块名, 方法名, 是否 commit, 是否 flush)，仅含改动数据的方法。"""
    out: list[tuple[str, str, bool, bool]] = []
    for path in _repo_modules():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for cls in (n for n in tree.body if isinstance(n, ast.ClassDef)):
            for fn in (n for n in cls.body if isinstance(n, ast.AsyncFunctionDef)):
                if _mutates(fn):
                    out.append((path.stem, fn.name, _calls(fn, "commit"), _calls(fn, "flush")))
    return out


def test_扫描到了写方法() -> None:
    """自检：解析器若失灵会一个都扫不到，那后面的断言就成了空转。"""
    methods = _write_methods()
    assert len(methods) >= 12, f"写方法数量异常，解析可能失灵: {methods}"


@pytest.mark.parametrize("module,method,has_commit,has_flush", _write_methods(), ids=lambda v: str(v))
def test_写方法不得提交(module: str, method: str, has_commit: bool, has_flush: bool) -> None:
    if module in _PENDING_UOW:
        pytest.xfail(f"{module} 尚未按 UoW 改造（plan-memory-outbox.md §6 第 3 步）")
    assert not has_commit, (
        f"{module}.{method} 自行 commit 了 —— 事务边界由 UnitOfWork 在用例层管理。"
        f"把 commit 换成 flush：同 session 内后续读仍可见，但整组写入能一起回滚。"
    )


@pytest.mark.parametrize("module,method,has_commit,has_flush", _write_methods(), ids=lambda v: str(v))
def test_写方法必须flush(module: str, method: str, has_commit: bool, has_flush: bool) -> None:
    """光不 commit 不够，还得 flush。

    只 add/merge 不 flush 的话，改动留在 session 的 identity map 里，SQL 还没发出
    —— 同一事务内后续按 id 读会拿不到（`supersede` 正是这个形态），且约束冲突
    要等到边界提交时才暴露，错误堆栈指不回真正出问题的那次写入。
    """
    if module in _PENDING_UOW:
        pytest.xfail(f"{module} 尚未按 UoW 改造（plan-memory-outbox.md §6 第 3 步）")
    assert has_flush, f"{module}.{method} 改了数据但没 flush"


def test_只读方法不提交也不flush() -> None:
    """反向约束：查询方法两者都不该调，否则白跑一次往返。"""
    offenders: list[str] = []
    for path in _repo_modules():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for cls in (n for n in tree.body if isinstance(n, ast.ClassDef)):
            for fn in (n for n in cls.body if isinstance(n, ast.AsyncFunctionDef)):
                if not _mutates(fn) and (_calls(fn, "commit") or _calls(fn, "flush")):
                    offenders.append(f"{path.stem}.{fn.name}")
    assert not offenders, f"只读方法不该 commit/flush: {offenders}"


def test_pending集合不含已改造的模块() -> None:
    """防止 _PENDING_UOW 变成僵尸豁免：已经不 commit 的模块必须从里面删掉。

    没有这条，改造完某个仓储却忘了删豁免，xfail 会静默吃掉它此后的回归。
    """
    still_committing = {m for m, _, has_commit, _ in _write_methods() if has_commit}
    stale = _PENDING_UOW - still_committing
    assert not stale, f"这些模块已不再 commit，请从 _PENDING_UOW 删除: {sorted(stale)}"
