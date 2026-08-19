"""手工写入的冲突检查（Admin API 的 409 契约）。

锁的缺陷：`POST /channels/{id}/memories` 此前不做任何近似检索。人在控制台写
「超时 5 秒」时库里有蒸馏出的「超时 3 秒」，两条直接并列成为现行，而**没有任何
一处会发现** —— 蒸馏侧那套 ADD / UPDATE / NOOP 只覆盖它自己那条路。

这一组打真路由 + 真 `MemoryService`（仓储用替身），因为要验的正是两者之间的契约：
service 返回 `ConflictCheck`、路由把它翻成 409 的 body 形状，前端按那个形状渲染
候选列表。只测 service 会漏掉序列化，只测路由要 mock 掉被测逻辑本身。

不挂整个 app：那会连真库（见 test_admin_auth.py 的 fixture），而这里与鉴权、
建表都无关。
"""

from __future__ import annotations

from collections.abc import Iterator
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from teamai.adapters.admin.memory import build_memory_router
from teamai.application.memory import MemoryService
from teamai.domain.models import MemoryEntry, MemorySource, MemoryType
from teamai.domain.services import AuditLogWriter
from teamai.infrastructure.uow import NullUnitOfWork
from tests.fakes import (
    FakeAuditRepository,
    FakeChannelRepository,
    FakeMemoryRepository,
    FakeOutboxRepository,
)
from tests.unit.test_memory import StubEmbedder, StubVectorStore

CH = "ch_1"


def _client(
    repo: FakeMemoryRepository,
    *,
    vector=None,
    embedder=None,
) -> tuple[TestClient, MemoryService, FakeAuditRepository]:
    audit_repo = FakeAuditRepository()
    service = MemoryService(
        repo,
        FakeChannelRepository(),
        AuditLogWriter(audit_repo),
        FakeOutboxRepository(),
        NullUnitOfWork(),
        vector_store=vector,
        embedder=embedder,
    )
    app = FastAPI()
    app.include_router(build_memory_router(SimpleNamespace(memory=service)), prefix="/api")
    return TestClient(app), service, audit_repo


@pytest.fixture
def repo() -> FakeMemoryRepository:
    return FakeMemoryRepository()


@pytest.fixture
def seeded(repo: FakeMemoryRepository) -> Iterator[FakeMemoryRepository]:
    """库里已有一条蒸馏产出的「超时 3 秒」。"""
    repo.stored.append(
        MemoryEntry(
            id="mem_old",
            channel_instance_id=CH,
            content="网关重试超时设为 3 秒",
            type=MemoryType.FACT,
            source=MemorySource.DISTILLED,
        )
    )
    yield repo


# ===== 409 =====


def test_疑似冲突时返回409且不写库(seeded: FakeMemoryRepository) -> None:
    client, _, _ = _client(
        seeded, vector=StubVectorStore([("mem_old", 0.93)]), embedder=StubEmbedder()
    )

    resp = client.post(f"/api/channels/{CH}/memories", json={"content": "网关重试超时设为 5 秒"})

    assert resp.status_code == 409
    assert len(seeded.stored) == 1, "409 时一条都不该写进去"


def test_409的body形状(seeded: FakeMemoryRepository) -> None:
    """前端按这个形状渲染候选列表（web/src/api/types.ts 的 MemoryConflict）。
    改这里必须同步改那边。"""
    client, _, _ = _client(
        seeded, vector=StubVectorStore([("mem_old", 0.93)]), embedder=StubEmbedder()
    )

    detail = client.post(
        f"/api/channels/{CH}/memories", json={"content": "网关重试超时设为 5 秒"}
    ).json()["detail"]

    assert detail["degraded"] is False
    assert isinstance(detail["message"], str) and detail["message"], "要有一句人话给前端显示"
    assert len(detail["conflicts"]) == 1
    conflict = detail["conflicts"][0]
    assert conflict["score"] == pytest.approx(0.93)
    # 候选是完整的记忆对象：前端要显示写入日期（那是判断哪条现行的依据）与类型
    assert conflict["entry"]["id"] == "mem_old"
    assert conflict["entry"]["content"] == "网关重试超时设为 3 秒"
    assert "created_at" in conflict["entry"]
    assert conflict["entry"]["type"] == "FACT"


def test_无冲突时正常写入(repo: FakeMemoryRepository) -> None:
    client, _, _ = _client(repo, vector=StubVectorStore(), embedder=StubEmbedder())

    resp = client.post(f"/api/channels/{CH}/memories", json={"content": "部署走 GitHub Actions"})

    assert resp.status_code == 200
    assert resp.json()["content"] == "部署走 GitHub Actions"
    assert len(repo.stored) == 1


def test_降级时409带degraded标记(seeded: FakeMemoryRepository) -> None:
    """未配 embedding（默认装配）时只能查字面重复。录入人得知道自己拿到的是
    什么 —— 不说的话「没报冲突」会被读成「确认没冲突」。"""
    client, _, _ = _client(seeded)  # 无 embedder

    detail = client.post(
        f"/api/channels/{CH}/memories", json={"content": "网关重试超时设为 3 秒。"}
    ).json()["detail"]

    assert detail["degraded"] is True
    assert detail["conflicts"][0]["score"] is None, "字面路径没有相似度，不能编一个"
    assert "字面" in detail["message"], "要在文案里说清这次只查了字面"


# ===== force =====


def test_force跳过检查并列写入(seeded: FakeMemoryRepository) -> None:
    client, _, _ = _client(
        seeded, vector=StubVectorStore([("mem_old", 0.99)]), embedder=StubEmbedder()
    )

    resp = client.post(
        f"/api/channels/{CH}/memories",
        json={"content": "网关重试超时设为 5 秒", "force": True},
    )

    assert resp.status_code == 200
    assert len(seeded.stored) == 2, "两条并列共存 —— 这是录入人明确要的"
    assert all(e.is_current for e in seeded.stored)


# ===== supersede =====


def test_supersede_id取代旧条目(seeded: FakeMemoryRepository) -> None:
    client, _, _ = _client(
        seeded, vector=StubVectorStore([("mem_old", 0.93)]), embedder=StubEmbedder()
    )

    resp = client.post(
        f"/api/channels/{CH}/memories",
        json={"content": "网关重试超时设为 5 秒", "supersede_id": "mem_old"},
    )

    assert resp.status_code == 200
    new_id = resp.json()["id"]
    old = next(e for e in seeded.stored if e.id == "mem_old")
    assert old.superseded_by == new_id, "旧条目要打上取代标记"
    assert old.superseded_at is not None
    assert not old.is_current
    # 旧条目**不删**：排查「机器人为什么这么说」时要看得到被取代的版本
    assert len(seeded.stored) == 2


def test_手工取代的source是MANUAL(seeded: FakeMemoryRepository) -> None:
    """不是 DISTILLED —— supersede 的默认 source 是给蒸馏用的。这条记忆是人写的，
    「这句话是谁写的」是出问题时第一个要问的（见 MemorySource 的文档）。"""
    client, _, _ = _client(
        seeded, vector=StubVectorStore([("mem_old", 0.93)]), embedder=StubEmbedder()
    )

    resp = client.post(
        f"/api/channels/{CH}/memories",
        json={"content": "网关重试超时设为 5 秒", "supersede_id": "mem_old"},
    )

    assert resp.json()["source"] == "MANUAL"


def test_取代不存在的id报400(repo: FakeMemoryRepository) -> None:
    client, _, _ = _client(repo, vector=StubVectorStore(), embedder=StubEmbedder())

    resp = client.post(
        f"/api/channels/{CH}/memories",
        json={"content": "新说法", "supersede_id": "mem_nope"},
    )

    assert resp.status_code == 400
    assert "mem_nope" in resp.json()["detail"]


def test_跨频道取代报400而非404(repo: FakeMemoryRepository) -> None:
    """频道隔离。合成一条 400 而不是分开报 404/403：两者对调用方是同一件事
    （这个 id 在这里用不了），分开报会把「B 频道有这个 id」透给 A 频道。"""
    repo.stored.append(
        MemoryEntry(id="mem_b", channel_instance_id="ch_B", content="别的频道的记忆")
    )
    client, _, _ = _client(repo, vector=StubVectorStore(), embedder=StubEmbedder())

    resp = client.post(
        f"/api/channels/{CH}/memories",
        json={"content": "新说法", "supersede_id": "mem_b"},
    )

    assert resp.status_code == 400
    b = next(e for e in repo.stored if e.id == "mem_b")
    assert b.is_current, "别的频道那条不能被动过"


def test_同时给force与supersede_id报400(seeded: FakeMemoryRepository) -> None:
    """不定优先级：两者表达相反的意图，同时给说明调用方没想清楚。静默择一会让
    另一半意图无声地丢掉。"""
    client, _, _ = _client(seeded, vector=StubVectorStore(), embedder=StubEmbedder())

    resp = client.post(
        f"/api/channels/{CH}/memories",
        json={"content": "新说法", "supersede_id": "mem_old", "force": True},
    )

    assert resp.status_code == 400
    assert len(seeded.stored) == 1


# ===== 偏好 =====


def test_写偏好不触发冲突检查(repo: FakeMemoryRepository) -> None:
    """偏好不建向量，语义检查对它结构性无效（见 find_conflicts 的文档）。
    这里验它不会因此被拦住 —— 结构性查不了的东西不该变成写不进去。"""
    repo.stored.append(
        MemoryEntry(
            id="mem_p",
            channel_instance_id=CH,
            content="回答要简短",
            type=MemoryType.PREFERENCE,
        )
    )
    client, _, _ = _client(repo, vector=StubVectorStore([("mem_p", 0.99)]), embedder=StubEmbedder())

    resp = client.post(
        f"/api/channels/{CH}/memories",
        json={"content": "回答要简短", "type": "PREFERENCE"},
    )

    assert resp.status_code == 200
    assert len(repo.stored) == 2


# ===== 既有行为不受影响 =====


def test_content为空仍报400(repo: FakeMemoryRepository) -> None:
    client, _, _ = _client(repo, vector=StubVectorStore(), embedder=StubEmbedder())

    resp = client.post(f"/api/channels/{CH}/memories", json={"content": ""})

    assert resp.status_code == 400


def test_type非法仍报400(repo: FakeMemoryRepository) -> None:
    """校验顺序：type 非法要在冲突检查之前报出来，否则人先看到一堆候选、
    选完才发现类型填错了。"""
    client, _, _ = _client(repo, vector=StubVectorStore(), embedder=StubEmbedder())

    resp = client.post(
        f"/api/channels/{CH}/memories", json={"content": "内容", "type": "NOPE"}
    )

    assert resp.status_code == 400
    assert "type 非法" in resp.json()["detail"]
