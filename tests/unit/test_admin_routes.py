"""Admin API 路由注册完整性校验。

admin_api.py 拆成 admin/ 后多了一个隐式依赖：每个资源模块都得在
build_admin_router 里 include_router，漏一个该组路由就静默不注册 ——
不报错、不告警，只在请求时 404。拆分前这 12 条里只有 /health 被测到，
这里把完整路由表固化成断言。
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from app.backend.main import create_app

ADMIN_DIR = pathlib.Path(__file__).resolve().parents[2] / "src" / "teamai" / "adapters" / "admin"

# 拆分前后逐条比对确认过的完整路由表，新增路由时同步更新
EXPECTED_ROUTES = {
    ("DELETE", "/api/memories/{entry_id}"),
    ("GET", "/api/channels"),
    ("GET", "/api/channels/{channel_instance_id}"),
    ("PATCH", "/api/channels/{channel_instance_id}"),
    ("GET", "/api/channels/{channel_instance_id}/audit"),
    ("GET", "/api/channels/{channel_instance_id}/budget"),
    ("GET", "/api/channels/{channel_instance_id}/memories"),
    ("GET", "/api/channels/{channel_instance_id}/policy"),
    ("GET", "/api/channels/{channel_instance_id}/tags"),
    ("GET", "/api/channels/{channel_instance_id}/tasks"),
    ("GET", "/api/health"),
    ("GET", "/api/tools"),
    ("POST", "/api/channels/{channel_instance_id}/memories"),
    ("POST", "/api/channels/{channel_instance_id}/tags"),
    ("PATCH", "/api/channels/{channel_instance_id}/tags/{tag_id}"),
    ("DELETE", "/api/channels/{channel_instance_id}/tags/{tag_id}"),
    ("PUT", "/api/channels/{channel_instance_id}/budget"),
    ("PUT", "/api/channels/{channel_instance_id}/policy"),
}


def _actual_routes() -> set[tuple[str, str]]:
    """从 OpenAPI schema 取路由表。

    不遍历 app.routes：FastAPI 把 include_router 存成惰性对象，既无 .path
    也不摊平到父列表，静态遍历会漏掉全部子路由（只剩 /api/health）。
    生成 schema 时子路由才被解析，故以 schema 为准。
    """
    schema = create_app().openapi()
    return {
        (method.upper(), path)
        for path, ops in schema.get("paths", {}).items()
        if path.startswith("/api")
        for method in ops
    }


def _resource_modules() -> list[str]:
    """admin/ 下的资源模块名。

    serializers（字段形状）与 auth（令牌校验依赖）不导出 build_*_router，
    故排除在外，否则会被误判成漏装的资源组。
    """
    skip = {"__init__", "serializers", "auth"}
    return sorted(p.stem for p in ADMIN_DIR.glob("*.py") if p.stem not in skip)


def _builders_called_by_init() -> set[str]:
    """静态解析 __init__.py，取出被 include_router 组装的 build_* 名字。

    用 AST 而非运行时反射：漏装的模块在运行时根本不出现，反射看不出缺了谁。
    """
    tree = ast.parse((ADMIN_DIR / "__init__.py").read_text(encoding="utf-8"))
    return {n.id for n in ast.walk(tree) if isinstance(n, ast.Name) and n.id.startswith("build_")}


def test_admin_目录有资源模块() -> None:
    assert ADMIN_DIR.is_dir(), f"未找到 admin 目录: {ADMIN_DIR}"
    assert _resource_modules(), "admin 目录下没有资源模块"


@pytest.mark.parametrize("module", _resource_modules())
def test_每个资源模块都被组装(module: str) -> None:
    expected = f"build_{module}_router"
    called = _builders_called_by_init()
    assert expected in called, (
        f"admin/{module}.py 未被 build_admin_router 组装，该组路由不会注册。"
        f"\n__init__.py 里出现的 builder: {sorted(called)}"
    )


def test_路由表与期望一致() -> None:
    actual = _actual_routes()
    assert actual == EXPECTED_ROUTES, (
        "Admin 路由表与期望不符。"
        f"\n缺失: {sorted(EXPECTED_ROUTES - actual)}"
        f"\n多出: {sorted(actual - EXPECTED_ROUTES)}"
    )
