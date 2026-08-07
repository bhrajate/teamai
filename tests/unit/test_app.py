"""ASGI 装配与 lifespan 的单元测试。

重点固化本次重构的三条结论：
1. Slack 只在凭据齐备时接入，缺凭据只跑 Admin API
2. Socket Mode 与 Events API 二选一，Socket Mode 下不挂 HTTP 路由
3. Socket Mode 客户端作为 lifespan 后台任务启停，退出时不留残余任务
"""

from __future__ import annotations

import asyncio

import pytest

import app.backend.main as web_main
import teamai.container as container_module
from app.backend.main import create_app
from teamai.config import settings


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch):
    """隔离全局容器与 settings，并跳过真实建表。"""
    container_module.reset_container()

    async def _noop() -> None:
        return None

    # 打 web_main 上的引用而非 db 模块：create_app 的 lifespan 按模块级名字调用
    monkeypatch.setattr(web_main, "init_db_or_warn", _noop)
    monkeypatch.setattr(settings, "slack_bot_token", "", raising=False)
    monkeypatch.setattr(settings, "slack_signing_secret", "", raising=False)
    monkeypatch.setattr(settings, "slack_app_token", "", raising=False)
    yield
    container_module.reset_container()


def _paths(app) -> set[str]:  # type: ignore[no-untyped-def]
    out: set[str] = set()
    for route in app.routes:
        path = getattr(route, "path", None)
        if path:
            out.add(path)
    return out


def _enable_slack(monkeypatch: pytest.MonkeyPatch, *, socket_mode: bool) -> None:
    monkeypatch.setattr(settings, "slack_bot_token", "xoxb-fake", raising=False)
    monkeypatch.setattr(settings, "slack_signing_secret", "signing-fake", raising=False)
    monkeypatch.setattr(settings, "slack_app_token", "xapp-fake" if socket_mode else "", raising=False)


def test_无凭据时不接入slack() -> None:
    assert settings.slack_enabled is False
    assert "/slack/events" not in _paths(create_app())


def test_仅配app_token不算凭据齐备(monkeypatch: pytest.MonkeyPatch) -> None:
    """app_token 只决定接入方式，不能顶替 bot_token/signing_secret。"""
    monkeypatch.setattr(settings, "slack_app_token", "xapp-fake", raising=False)
    assert settings.slack_enabled is False


def test_events_api模式挂载http入口(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_slack(monkeypatch, socket_mode=False)
    assert settings.slack_enabled is True
    assert "/slack/events" in _paths(create_app())


def test_socket_mode不挂载http入口(monkeypatch: pytest.MonkeyPatch) -> None:
    """Socket Mode 走 WS 收事件，挂 HTTP 路由是死代码且会误导运维配回调地址。"""
    _enable_slack(monkeypatch, socket_mode=True)
    assert "/slack/events" not in _paths(create_app())


def test_admin_api路由始终存在() -> None:
    from fastapi.testclient import TestClient

    with TestClient(create_app()) as client:
        assert client.get("/api/health").json() == {"status": "ok"}


def test_容器在进程内复用() -> None:
    assert container_module.get_container() is container_module.get_container()


async def test_socket_mode任务随lifespan启停(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_slack(monkeypatch, socket_mode=True)

    started = asyncio.Event()
    closed = asyncio.Event()

    class FakeHandler:
        async def start_async(self) -> None:
            started.set()
            await asyncio.sleep(3600)  # 模拟长连接阻塞

        async def close_async(self) -> None:
            closed.set()

    monkeypatch.setattr(
        "teamai.adapters.slack_app.build_socket_mode_handler",
        lambda app: FakeHandler(),
    )

    app = create_app()
    async with app.router.lifespan_context(app):
        await asyncio.wait_for(started.wait(), timeout=1.0)
        names = {t.get_name() for t in asyncio.all_tasks()}
        assert "slack-socket-mode" in names

    assert closed.is_set(), "退出时须调用 close_async 断开长连接"
    leftover = [t for t in asyncio.all_tasks() if t.get_name() == "slack-socket-mode" and not t.done()]
    assert leftover == [], "lifespan 退出后不应残留 socket 任务"


async def test_events_api模式不起socket任务(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_slack(monkeypatch, socket_mode=False)

    app = create_app()
    async with app.router.lifespan_context(app):
        names = {t.get_name() for t in asyncio.all_tasks()}
        assert "slack-socket-mode" not in names


async def test_socket_mode关闭异常不阻断退出(monkeypatch: pytest.MonkeyPatch) -> None:
    """退出路径是尽力而为：断连失败也要让进程正常退出。"""
    _enable_slack(monkeypatch, socket_mode=True)

    class BrokenHandler:
        async def start_async(self) -> None:
            await asyncio.sleep(3600)

        async def close_async(self) -> None:
            raise RuntimeError("断连失败（测试注入）")

    monkeypatch.setattr(
        "teamai.adapters.slack_app.build_socket_mode_handler",
        lambda app: BrokenHandler(),
    )

    app = create_app()
    async with app.router.lifespan_context(app):
        await asyncio.sleep(0.01)
    # 未抛异常即通过
