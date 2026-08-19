"""共享 fixture。"""

from __future__ import annotations

import asyncio
import socket
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
import uvicorn
from fastmcp import FastMCP

from teamai.application.orchestrator import TaskOrchestrator
from teamai.domain.services import AuditLogWriter
from tests.fakes import FakeAuditRepository, FakeTaskQueue, FakeTaskRepository


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def build_mcp_server() -> FastMCP:
    """测试用的 FastMCP server：三个工具（成功 / 报错各路径）。"""
    mcp = FastMCP("test-server")

    @mcp.tool()
    async def add(a: int, b: int) -> int:
        """Add two numbers."""
        return a + b

    @mcp.tool()
    async def greet(name: str) -> str:
        """Greet someone."""
        return f"hello {name}"

    @mcp.tool()
    async def boom() -> str:
        """Always fails."""
        raise ValueError("内部分裂")

    return mcp


@pytest_asyncio.fixture
async def mcp_server_url() -> AsyncIterator[str]:
    """起一个 uvicorn 托管的 FastMCP server，返回 /mcp 端点 URL。

    streamable HTTP 的会话协商依赖真实 HTTP 语义（initialize / 消息通道），
    ASGI in-memory transport 在此不通，起真实端口是本测试最廉价的验证。
    """
    port = free_port()
    cfg = uvicorn.Config(
        build_mcp_server().http_app(), host="127.0.0.1", port=port, log_level="error"
    )
    server = uvicorn.Server(cfg)
    task = asyncio.create_task(server.serve())
    for _ in range(100):
        if server.started:
            break
        await asyncio.sleep(0.05)
    yield f"http://127.0.0.1:{port}/mcp"
    server.should_exit = True
    await task


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
