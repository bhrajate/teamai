"""全局审计流水端点。

重点不是「端点能不能返回数据」，而是**隔离**：全局变更不能出现在任何频道的
流水里，频道变更也不能出现在全局流水里。这个隔离只靠 GLOBAL_SCOPE 这一个取值
约定维持，没有类型或约束兜底，故必须有测试。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from teamai.adapters.admin.audit import build_audit_router
from teamai.domain.models import GLOBAL_SCOPE, AuditAction, AuditLog


class FakeAuditRepo:
    """按 channel_instance_id 过滤 + 时间倒序，语义对齐 SQL 实现。"""

    def __init__(self) -> None:
        self.logs: list[AuditLog] = []

    async def append(self, log: AuditLog) -> None:
        self.logs.append(log)

    async def list_by_channel(self, channel_instance_id: str, limit: int = 100) -> list[AuditLog]:
        rows = [x for x in self.logs if x.channel_instance_id == channel_instance_id]
        return sorted(rows, key=lambda x: x.ts, reverse=True)[:limit]


@dataclass
class FakeContainer:
    audit_repo: FakeAuditRepo = field(default_factory=FakeAuditRepo)


def _log(scope: str, event: str, minutes_ago: int = 0) -> AuditLog:
    return AuditLog(
        id=f"audit_{event}_{minutes_ago}",
        channel_instance_id=scope,
        user_id="u1",
        action=AuditAction.POLICY_CHANGE,
        detail={"event": event},
        ts=datetime.now(UTC) - timedelta(minutes=minutes_ago),
    )


@pytest.fixture
def repo() -> FakeAuditRepo:
    return FakeAuditRepo()


@pytest.fixture
def client(repo: FakeAuditRepo) -> AsyncIterator[TestClient]:
    app = FastAPI()
    app.include_router(build_audit_router(FakeContainer(audit_repo=repo)))
    yield TestClient(app)


def test_全局端点只返回全局记录(client: TestClient, repo: FakeAuditRepo) -> None:
    # TestClient 是同步的，种子数据直接塞进 fake 的列表，不走 async append
    repo.logs.extend(
        [
            _log(GLOBAL_SCOPE, "skill_create", 3),
            _log(GLOBAL_SCOPE, "skill_file_create", 2),
            _log("ch_1", "channel_skills_set", 1),
        ]
    )

    body = client.get("/audit/global").json()

    assert {x["detail"]["event"] for x in body} == {"skill_create", "skill_file_create"}
    assert all(x["channel_instance_id"] == GLOBAL_SCOPE for x in body)


def test_频道端点不含全局记录(client: TestClient, repo: FakeAuditRepo) -> None:
    """反向隔离：全局变更混进频道流水会让「这个频道发生了什么」失真。"""
    repo.logs.extend(
        [_log(GLOBAL_SCOPE, "skill_create"), _log("ch_1", "channel_skills_set")]
    )

    body = client.get("/channels/ch_1/audit").json()

    assert [x["detail"]["event"] for x in body] == ["channel_skills_set"]


def test_全局流水为空时返回空列表(client: TestClient) -> None:
    assert client.get("/audit/global").json() == []


def test_全局流水按时间倒序(client: TestClient, repo: FakeAuditRepo) -> None:
    repo.logs.extend(
        [
            _log(GLOBAL_SCOPE, "最早", 10),
            _log(GLOBAL_SCOPE, "最晚", 0),
            _log(GLOBAL_SCOPE, "中间", 5),
        ]
    )

    body = client.get("/audit/global").json()

    assert [x["detail"]["event"] for x in body] == ["最晚", "中间", "最早"]


def test_limit生效(client: TestClient, repo: FakeAuditRepo) -> None:
    repo.logs.extend(_log(GLOBAL_SCOPE, f"e{i}", i) for i in range(10))

    assert len(client.get("/audit/global", params={"limit": 3}).json()) == 3


def test_global不会被当成频道id(client: TestClient, repo: FakeAuditRepo) -> None:
    """``/channels/global/audit`` 与 ``/audit/global`` 恰好同源，但前者是巧合而非
    契约 —— 前端只该用后者。这里锁住：真实频道 id 由 gen_id("ch") 生成
    （``ch_<26 位 ULID>``），永远不会等于 "global"，故两者不会互相污染。
    """
    repo.logs.extend([_log(GLOBAL_SCOPE, "skill_create"), _log("ch_01K", "policy_change")])

    assert [x["detail"]["event"] for x in client.get("/audit/global").json()] == ["skill_create"]
    assert [
        x["detail"]["event"] for x in client.get("/channels/ch_01K/audit").json()
    ] == ["policy_change"]
