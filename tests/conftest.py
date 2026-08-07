"""共享 fixture。"""

from __future__ import annotations

import pytest

from teamai.application.orchestrator import TaskOrchestrator
from teamai.domain.services import AuditLogWriter
from tests.fakes import FakeAuditRepository, FakeTaskQueue, FakeTaskRepository


@pytest.fixture
def task_repo() -> FakeTaskRepository:
    return FakeTaskRepository()


@pytest.fixture
def audit_repo() -> FakeAuditRepository:
    return FakeAuditRepository()


@pytest.fixture
def queue() -> FakeTaskQueue:
    return FakeTaskQueue()


@pytest.fixture
def audit(audit_repo: FakeAuditRepository) -> AuditLogWriter:
    return AuditLogWriter(audit_repo)


@pytest.fixture
def orchestrator(
    task_repo: FakeTaskRepository,
    audit: AuditLogWriter,
    queue: FakeTaskQueue,
) -> TaskOrchestrator:
    return TaskOrchestrator(task_repo, audit, queue)
