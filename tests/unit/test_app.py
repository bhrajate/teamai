"""ASGI 装配与 lifespan 的单元测试。

阶段二起为矩阵形态：平台 × 接入模式 × 是否配凭据。当前矩阵只有 Slack；
飞书接入（阶段三/四）后在此补 feishu 行列。

固化三条结论：
1. 平台只在凭据齐备时接入，缺凭据只跑 Admin API
2. 接入模式二选一：auto 按 app_token 推断（旧行为），显式 mode 优先
3. 长连接客户端作为 lifespan 后台任务启停，退出时不留残余任务
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

    monkeypatch.setattr(web_main, "init_db_or_warn", _noop)
    _disable_slack(monkeypatch)
    _disable_feishu(monkeypatch)
    yield
    container_module.reset_container()


def _disable_slack(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "slack_bot_token", "", raising=False)
    monkeypatch.setattr(settings, "slack_signing_secret", "", raising=False)
    monkeypatch.setattr(settings, "slack_app_token", "", raising=False)
    monkeypatch.setattr(settings, "platforms_slack_mode", "auto", raising=False)


def _enable_slack(monkeypatch: pytest.MonkeyPatch, *, mode: str = "auto") -> None:
    _disable_slack(monkeypatch)
    monkeypatch.setattr(settings, "slack_bot_token", "xoxb-fake", raising=False)
    monkeypatch.setattr(settings, "slack_signing_secret", "signing-fake", raising=False)
    monkeypatch.setattr(settings, "platforms_slack_mode", mode, raising=False)


def _disable_feishu(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "feishu_app_id", "", raising=False)
    monkeypatch.setattr(settings, "feishu_app_secret", "", raising=False)
    monkeypatch.setattr(settings, "feishu_encrypt_key", "", raising=False)
    monkeypatch.setattr(settings, "feishu_verification_token", "", raising=False)
    monkeypatch.setattr(settings, "platforms_feishu_mode", "auto", raising=False)
    monkeypatch.setattr(settings, "platforms_feishu_domain", "feishu", raising=False)


def _enable_feishu(monkeypatch: pytest.MonkeyPatch, *, mode: str = "auto") -> None:
    _disable_feishu(monkeypatch)
    monkeypatch.setattr(settings, "feishu_app_id", "cli_fake", raising=False)
    monkeypatch.setattr(settings, "feishu_app_secret", "secret-fake", raising=False)
    monkeypatch.setattr(settings, "platforms_feishu_mode", mode, raising=False)


def _paths(app) -> set[str]:  # type: ignore[no-untyped-def]
    out: set[str] = set()
    for route in app.routes:
        path = getattr(route, "path", None)
        if path:
            out.add(path)
    return out


# ---------- 凭据门槛 ----------

def test_无凭据时不接入任何平台() -> None:
    assert settings.slack_enabled is False
    assert settings.feishu_enabled is False
    paths = _paths(create_app())
    assert "/slack/events" not in paths
    assert "/feishu/events" not in paths


def test_仅配app_token不算凭据齐备(monkeypatch: pytest.MonkeyPatch) -> None:
    """app_token 只决定接入方式，不能顶替 bot_token/signing_secret。"""
    monkeypatch.setattr(settings, "slack_app_token", "xapp-fake", raising=False)
    assert settings.slack_enabled is False


# ---------- 接入模式解析 ----------

@pytest.mark.parametrize(
    ("mode_cfg", "app_token", "expected"),
    [
        ("auto", "", "events"),  # 旧行为：无 app_token → Events API
        ("auto", "xapp-fake", "socket"),  # 旧行为：有 app_token → Socket Mode
        ("events", "xapp-fake", "events"),  # 显式 mode 优先于 app_token 推断
        ("socket", "", "socket"),
    ],
)
def test_slack_mode解析(mode_cfg: str, app_token: str, expected: str, monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_slack(monkeypatch, mode=mode_cfg)
    monkeypatch.setattr(settings, "slack_app_token", app_token, raising=False)

    from teamai.adapters.slack.app import SlackConnector

    assert SlackConnector(object(), object())._mode() == expected  # type: ignore[arg-type]


def test_events_api模式挂载http入口(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_slack(monkeypatch, mode="events")
    assert settings.slack_enabled is True
    assert "/slack/events" in _paths(create_app())


def test_socket_mode不挂载http入口(monkeypatch: pytest.MonkeyPatch) -> None:
    """Socket Mode 走 WS 收事件，挂 HTTP 路由是死代码且会误导运维配回调地址。"""
    _enable_slack(monkeypatch, mode="socket")
    assert "/slack/events" not in _paths(create_app())


@pytest.mark.parametrize(
    ("app_token", "expect_http"),
    [("", True), ("xapp-fake", False)],
)
def test_auto模式按app_token决定路由挂载(
    app_token: str, expect_http: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_slack(monkeypatch, mode="auto")
    monkeypatch.setattr(settings, "slack_app_token", app_token, raising=False)
    assert ("/slack/events" in _paths(create_app())) is expect_http


# ---------- Admin API 与容器 ----------

def test_admin_api路由始终存在() -> None:
    from fastapi.testclient import TestClient

    with TestClient(create_app()) as client:
        assert client.get("/api/health").json() == {"status": "ok"}


def test_容器在进程内复用() -> None:
    assert container_module.get_container() is container_module.get_container()


# ---------- lifespan 生命周期 ----------

async def test_socket_mode任务随lifespan启停(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_slack(monkeypatch, mode="socket")

    started = asyncio.Event()
    closed = asyncio.Event()

    class FakeHandler:
        async def start_async(self) -> None:
            started.set()
            await asyncio.sleep(3600)  # 模拟长连接阻塞

        async def close_async(self) -> None:
            closed.set()

    monkeypatch.setattr(
        "teamai.adapters.slack.app.build_socket_mode_handler",
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
    _enable_slack(monkeypatch, mode="events")

    app = create_app()
    async with app.router.lifespan_context(app):
        names = {t.get_name() for t in asyncio.all_tasks()}
        assert "slack-socket-mode" not in names


async def test_socket_mode关闭异常不阻断退出(monkeypatch: pytest.MonkeyPatch) -> None:
    """退出路径是尽力而为：断连失败也要让进程正常退出。"""
    _enable_slack(monkeypatch, mode="socket")

    class BrokenHandler:
        async def start_async(self) -> None:
            await asyncio.sleep(3600)

        async def close_async(self) -> None:
            raise RuntimeError("断连失败（测试注入）")

    monkeypatch.setattr(
        "teamai.adapters.slack.app.build_socket_mode_handler",
        lambda app: BrokenHandler(),
    )

    app = create_app()
    async with app.router.lifespan_context(app):
        await asyncio.sleep(0.01)
    # 未抛异常即通过


# ---------- 飞书接入方式 ----------

@pytest.mark.parametrize(
    ("mode_cfg", "encrypt_key", "v_token", "expected"),
    [
        ("auto", "", "", "ws"),  # 无 callback 凭据 → 长连接
        ("auto", "ek", "vt", "callback"),  # 配齐 callback 凭据 → HTTP 回调
        ("callback", "", "", "callback"),  # 显式 mode 优先于凭据推断
        ("ws", "ek", "vt", "ws"),
    ],
)
def test_feishu_mode解析(
    mode_cfg: str, encrypt_key: str, v_token: str, expected: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_feishu(monkeypatch, mode=mode_cfg)
    monkeypatch.setattr(settings, "feishu_encrypt_key", encrypt_key, raising=False)
    monkeypatch.setattr(settings, "feishu_verification_token", v_token, raising=False)

    from teamai.adapters.feishu.connector import FeishuConnector

    assert FeishuConnector(object(), object(), object())._mode() == expected  # type: ignore[arg-type]


def test_feishu_callback模式挂载http入口(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_feishu(monkeypatch, mode="callback")
    assert settings.feishu_enabled is True
    assert "/feishu/events" in _paths(create_app())


def test_feishu_ws模式不挂载http入口(monkeypatch: pytest.MonkeyPatch) -> None:
    """长连接走 WS 收事件，挂 HTTP 路由是死代码且会误导运维配回调地址。"""
    _enable_feishu(monkeypatch, mode="ws")
    assert "/feishu/events" not in _paths(create_app())


@pytest.mark.parametrize(
    ("encrypt_key", "v_token", "expect_http"),
    [
        ("ek", "vt", True),  # auto：配齐 callback 凭据 → 挂回调入口
        ("", "", False),  # auto：无 callback 凭据 → 长连接，不挂路由
    ],
)
def test_feishu_auto模式按凭据决定路由挂载(
    encrypt_key: str, v_token: str, expect_http: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_feishu(monkeypatch, mode="auto")
    monkeypatch.setattr(settings, "feishu_encrypt_key", encrypt_key, raising=False)
    monkeypatch.setattr(settings, "feishu_verification_token", v_token, raising=False)
    assert ("/feishu/events" in _paths(create_app())) is expect_http


async def test_feishu_ws模式lifespan建立会话(monkeypatch: pytest.MonkeyPatch) -> None:
    """长连接模式下 startup 拉取 bot 身份并启动 WS 会话；退出不报错。"""
    _enable_feishu(monkeypatch, mode="ws")

    started = asyncio.Event()

    class FakeIdentity:
        open_id = "ou_bot_fake"

    class FakeWsSession:
        def __init__(self, *a: object, **kw: object) -> None:
            pass

        def start(self) -> None:
            started.set()

    monkeypatch.setattr("teamai.adapters.feishu.connector.fetch_bot_identity", lambda config: FakeIdentity())
    # connector 用 `from teamai.adapters.feishu.ws import FeishuWsSession` 绑定，
    # 须 patch connector 命名空间里的名字而非 ws 模块
    monkeypatch.setattr("teamai.adapters.feishu.connector.FeishuWsSession", FakeWsSession)

    app = create_app()
    async with app.router.lifespan_context(app):
        await asyncio.wait_for(started.wait(), timeout=1.0)
    # 未抛异常即通过
