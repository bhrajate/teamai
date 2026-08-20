"""Admin API 令牌校验。

这组守两件事：未配令牌时旧部署照旧能用（不能因为加了鉴权就把内网跑着的实例锁死），
配了令牌后资源路由一律拦住 —— /api 上挂着可写的预算配额与工具白名单，
放开等于谁都能把配额调到无限、把工具白名单开满。

/health 有意不在保护范围内：探针与 make verify-* 要匿名可打。
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import app.backend.main as web_main
import teamai.container as container_module
from app.backend.main import create_app
from teamai.adapters.admin.auth import require_admin_token
from teamai.config import settings


@pytest.fixture
def _no_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "admin_api_token", "")


@pytest.fixture
def _with_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "admin_api_token", "s3cret")


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, _with_token: None) -> Iterator[TestClient]:
    """配了令牌的应用。不接平台、不建表 —— 本组只关心鉴权这一层。"""
    container_module.reset_container()

    async def _noop() -> None:
        return None

    monkeypatch.setattr(web_main, "init_db_or_warn", _noop)
    for name in ("slack_bot_token", "slack_signing_secret", "feishu_app_id", "feishu_app_secret"):
        monkeypatch.setattr(settings, name, "", raising=False)

    # raise_server_exceptions=False：过了鉴权的用例会走到 handler 里去连 DB，
    # 而本组有意不建表。默认设置下那个异常会被 TestClient 原样抛出，用例拿不到
    # 状态码，「200/500 都算通过」的约定也就无从判定 —— 且失败信息指向 DB 而非
    # 鉴权，与本组的关注点无关。
    with TestClient(create_app(), raise_server_exceptions=False) as c:
        yield c
    container_module.reset_container()


@pytest.mark.usefixtures("_no_token")
async def test_未配令牌时匿名放行() -> None:
    assert await require_admin_token(None) is None


@pytest.mark.usefixtures("_with_token")
async def test_令牌正确时放行() -> None:
    assert await require_admin_token("Bearer s3cret") is None


@pytest.mark.usefixtures("_with_token")
async def test_scheme大小写不敏感() -> None:
    """curl 与各家 HTTP 客户端写法不一，bearer/Bearer 都得认。"""
    assert await require_admin_token("bearer s3cret") is None


@pytest.mark.usefixtures("_with_token")
@pytest.mark.parametrize(
    "header",
    [
        None,  # 完全没带
        "",  # 空头
        "s3cret",  # 漏了 scheme
        "Bearer",  # 只有 scheme
        "Bearer ",  # scheme 后空令牌
        "Bearer wrong",  # 令牌不对
        "Bearer s3cre",  # 前缀正确但被截断
        "Bearer s3cret1",  # 正确令牌加了后缀
        "Basic s3cret",  # scheme 不对
    ],
)
async def test_令牌不合规一律401(header: str | None) -> None:
    with pytest.raises(HTTPException) as exc:
        await require_admin_token(header)
    assert exc.value.status_code == 401
    # 客户端靠这个头判断该走哪种认证
    assert exc.value.headers == {"WWW-Authenticate": "Bearer"}


@pytest.mark.usefixtures("_with_token")
@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/channels"),
        ("GET", "/api/channels/ch_x/tasks"),
        ("GET", "/api/channels/ch_x/audit"),
        ("GET", "/api/channels/ch_x/memories"),
        ("PUT", "/api/channels/ch_x/budget"),
        ("PUT", "/api/channels/ch_x/policy"),
        ("PATCH", "/api/channels/ch_x"),
        ("DELETE", "/api/memories/m_x"),
    ],
)
def test_资源路由无令牌一律401(client: TestClient, method: str, path: str) -> None:
    """401 在依赖阶段抛出，早于 handler，故本用例不碰 DB。

    逐条列举而非只测一条：令牌依赖挂在 include_router 上，漏挂某一组不会报错，
    只会让那组路由静默裸奔 —— 与 test_admin_routes.py 防的是同一类失误。
    """
    assert client.request(method, path).status_code == 401


def test_健康检查匿名可打(client: TestClient) -> None:
    """/health 若被一并保护，探针与 make verify-* 会全线 401。"""
    resp = client.get("/api/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_带对令牌可越过鉴权(client: TestClient) -> None:
    """只验「过了鉴权这一关」：401 消失即达标。

    过关后 handler 要连 DB，本用例不起容器，故 200/500 都算通过 ——
    真正的取数正确性由集成测试覆盖。
    """
    resp = client.get("/api/channels", headers={"Authorization": "Bearer s3cret"})
    assert resp.status_code != 401
