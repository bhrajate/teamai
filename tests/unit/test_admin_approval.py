"""审批的 Admin API：策略里的两个新字段 + 只读待批列表。

只读是有意的：Admin API 只有一个共享令牌，actor 是前端随便填的，而审批的审计链
不该建在不可信字段上。这里有一条专测锁住「没有放行端点」。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from teamai.adapters.admin.approval import build_approval_router
from teamai.adapters.admin.policy import build_policy_router
from teamai.domain.models import PendingApproval, PermissionPolicy, Task, TaskStatus
from teamai.domain.models.approval import ApprovalRecord


class FakePolicyRepo:
    def __init__(self) -> None:
        self.saved: PermissionPolicy | None = None

    async def get_for_channel(self, cid: str) -> PermissionPolicy | None:
        return self.saved

    async def upsert(self, policy: PermissionPolicy) -> None:
        self.saved = policy


class FakeTaskRepo:
    def __init__(self, tasks: list[Task] | None = None) -> None:
        self.items = {t.id: t for t in (tasks or [])}

    async def list_by_channel(self, cid: str, status: TaskStatus | None = None) -> list[Task]:
        out = [t for t in self.items.values() if t.channel_instance_id == cid]
        return [t for t in out if status is None or t.status is status]


class FakeCheckpointRepo:
    def __init__(self, pending: dict[str, PendingApproval] | None = None) -> None:
        self.pending = pending or {}

    async def get_pending_approval(self, task_id: str) -> PendingApproval | None:
        return self.pending.get(task_id)


class FakeTools:
    @property
    def names(self) -> list[str]:
        return ["github", "monitoring"]


@dataclass
class FakeContainer:
    policy_repo: FakePolicyRepo = field(default_factory=FakePolicyRepo)
    task_repo: FakeTaskRepo = field(default_factory=FakeTaskRepo)
    checkpoint_repo: FakeCheckpointRepo = field(default_factory=FakeCheckpointRepo)
    tools: FakeTools = field(default_factory=FakeTools)


def _task(tid: str = "task_1", **kw) -> Task:
    t = Task(
        id=tid,
        channel_instance_id="ch_1",
        thread_ref="ts_1",
        requester_id=kw.pop("requester_id", "U9"),
        intent="code_review",
    )
    t.status = kw.pop("status", TaskStatus.WAITING_INPUT)
    t.owner_id = kw.pop("owner_id", None)
    return t


@pytest.fixture
def container() -> FakeContainer:
    return FakeContainer()


@pytest.fixture
def client(container: FakeContainer) -> AsyncIterator[TestClient]:
    app = FastAPI()
    app.include_router(build_policy_router(container))
    app.include_router(build_approval_router(container))
    yield TestClient(app)


# ---- 策略里的审批配置 ----


def test_保存审批配置(client: TestClient, container: FakeContainer) -> None:
    r = client.put(
        "/channels/ch_1/policy",
        json={
            "allowed_tools": ["github"],
            "ambient_rules": [],
            "approval_required_tools": {"github": 1, "mcp__deploy": 2},
            "approver_ids": ["U1", "U2"],
        },
    )

    assert r.status_code == 200
    body = r.json()
    assert body["approval_required_tools"] == {"github": 1, "mcp__deploy": 2}
    assert body["approver_ids"] == ["U1", "U2"]
    assert container.policy_repo.saved is not None
    assert container.policy_repo.saved.approvals_needed("mcp__deploy__rollout") == 2


def test_不配审批时为空(client: TestClient) -> None:
    r = client.put("/channels/ch_1/policy", json={"allowed_tools": ["github"]})

    assert r.json()["approval_required_tools"] == {}
    assert r.json()["approver_ids"] == []


def test_配了审批工具却无审批人报422(client: TestClient) -> None:
    """那些工具将永远无法执行 —— 运行时会正确拒绝，但在这里挡住更省事。"""
    r = client.put(
        "/channels/ch_1/policy",
        json={"approval_required_tools": {"github": 1}, "approver_ids": []},
    )

    assert r.status_code == 422
    assert "永远无法执行" in r.json()["detail"]


def test_只配审批人不报错(client: TestClient) -> None:
    """先配人、后配工具是合理顺序。"""
    r = client.put("/channels/ch_1/policy", json={"approver_ids": ["U1"]})
    assert r.status_code == 200


@pytest.mark.parametrize("count", [0, -1])
def test_批准数须大于0(client: TestClient, count: int) -> None:
    r = client.put(
        "/channels/ch_1/policy",
        json={"approval_required_tools": {"github": count}, "approver_ids": ["U1"]},
    )
    assert r.status_code == 422
    assert ">= 1" in r.json()["detail"]


def test_批准数是字符串也接受(client: TestClient) -> None:
    """前端表单值常是字符串。"""
    r = client.put(
        "/channels/ch_1/policy",
        json={"approval_required_tools": {"github": "2"}, "approver_ids": ["U1"]},
    )
    assert r.status_code == 200
    assert r.json()["approval_required_tools"] == {"github": 2}


def test_批准数不是数字报422(client: TestClient) -> None:
    r = client.put(
        "/channels/ch_1/policy",
        json={"approval_required_tools": {"github": "很多"}, "approver_ids": ["U1"]},
    )
    assert r.status_code == 422


def test_审批配置不是对象报422(client: TestClient) -> None:
    r = client.put(
        "/channels/ch_1/policy",
        json={"approval_required_tools": ["github"], "approver_ids": ["U1"]},
    )
    assert r.status_code == 422


def test_空白审批人被过滤(client: TestClient) -> None:
    r = client.put("/channels/ch_1/policy", json={"approver_ids": ["U1", "", "  "]})
    assert r.json()["approver_ids"] == ["U1"]


def test_不校验工具是否已注册(client: TestClient) -> None:
    """MCP 工具在 worker 启动后才存在，而策略可以先配 —— 与 allowed_tools 的
    现有语义一致（未注册的名字被 registry 自然忽略）。"""
    r = client.put(
        "/channels/ch_1/policy",
        json={"approval_required_tools": {"mcp__future": 1}, "approver_ids": ["U1"]},
    )
    assert r.status_code == 200


# ---- 只读待批列表 ----


def test_列出待批(client: TestClient, container: FakeContainer) -> None:
    container.task_repo.items = {"task_1": _task(requester_id="U9", owner_id="U5")}
    container.checkpoint_repo.pending = {
        "task_1": PendingApproval(
            tool_call_id="tc_1",
            tool_name="github",
            args={"action": "create_pr", "title": "修 bug"},
            required=2,
            approvals=[ApprovalRecord(user_id="U1")],
        )
    }

    (item,) = client.get("/channels/ch_1/approvals").json()

    assert item["task_id"] == "task_1"
    assert item["tool_name"] == "github"
    assert item["args"] == {"action": "create_pr", "title": "修 bug"}
    assert item["required"] == 2
    assert item["approved_by"] == ["U1"]
    assert item["progress"] == "1/2"
    # 前端要能显示「谁发起的」—— 那正是不能批准它的那个人
    assert item["requester_id"] == "U9"
    assert item["owner_id"] == "U5"
    # 控制台不放行，用户看完要回线程 /approve，得知道是哪个线程
    assert item["thread_ref"] == "ts_1"


def test_参数不截断(client: TestClient, container: FakeContainer) -> None:
    """审批人必须看全参数才能判断，截断等于让人盲签。"""
    long_body = "x" * 5000
    container.task_repo.items = {"task_1": _task()}
    container.checkpoint_repo.pending = {
        "task_1": PendingApproval("tc", "github", args={"body": long_body})
    }

    (item,) = client.get("/channels/ch_1/approvals").json()

    assert item["args"]["body"] == long_body


def test_无待批时空列表(client: TestClient) -> None:
    assert client.get("/channels/ch_1/approvals").json() == []


def test_只列WAITING_INPUT的任务(client: TestClient, container: FakeContainer) -> None:
    container.task_repo.items = {
        "task_1": _task("task_1", status=TaskStatus.WAITING_INPUT),
        "task_2": _task("task_2", status=TaskStatus.RUNNING),
    }
    container.checkpoint_repo.pending = {
        "task_1": PendingApproval("tc1", "github"),
        "task_2": PendingApproval("tc2", "github"),
    }

    items = client.get("/channels/ch_1/approvals").json()

    assert [x["task_id"] for x in items] == ["task_1"]


def test_状态是WAITING_INPUT但无待批项时跳过(
    client: TestClient, container: FakeContainer
) -> None:
    """理论上不该出现，但不该因此 500。"""
    container.task_repo.items = {"task_1": _task()}
    container.checkpoint_repo.pending = {}

    assert client.get("/channels/ch_1/approvals").json() == []


def test_频道隔离(client: TestClient, container: FakeContainer) -> None:
    other = _task("task_other")
    other.channel_instance_id = "ch_other"
    container.task_repo.items = {"task_1": _task(), "task_other": other}
    container.checkpoint_repo.pending = {
        "task_1": PendingApproval("tc1", "github"),
        "task_other": PendingApproval("tc2", "github"),
    }

    items = client.get("/channels/ch_1/approvals").json()

    assert [x["task_id"] for x in items] == ["task_1"]


def test_没有放行端点() -> None:
    """**有意的**：Admin API 只有一个共享令牌，actor 是前端随便填的，而审批的
    审计链不该建在不可信字段上。放行必须回频道线程做（SPEC §6.4）。

    日后若要加，前置依赖是用户模型 —— 那时这条测试该连同 SPEC 一起改。
    """
    app = FastAPI()
    app.include_router(build_approval_router(FakeContainer()))
    methods = {
        (m, r.path) for r in app.routes for m in getattr(r, "methods", set())  # type: ignore[attr-defined]
    }
    writes = {(m, p) for m, p in methods if m in ("POST", "PUT", "PATCH", "DELETE")}
    assert writes == set(), f"审批路由不该有写端点: {writes}"
