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
    # 全局资源（skill 库）的变更流水。专门端点而非 /channels/global/audit：
    # GLOBAL_SCOPE 的取值约定不该泄到前端。
    ("GET", "/api/audit/global"),
    ("GET", "/api/channels/{channel_instance_id}/budget"),
    ("GET", "/api/channels/{channel_instance_id}/memories"),
    ("GET", "/api/channels/{channel_instance_id}/policy"),
    ("GET", "/api/channels/{channel_instance_id}/tags"),
    # 记忆的编辑走 PATCH（原地改，保留 id 与 created_at）。有意不支持改
    # visibility —— 那属权限变更而非内容编辑。
    ("PATCH", "/api/memories/{entry_id}"),
    # embedder 可用性。受令牌保护而非挂在匿名的 /health 上：「这个部署有没有配
    # embedding」是运营信息，与 /metrics 的取舍一致。控制台据此在记忆页提示降级。
    ("GET", "/api/embedding"),
    ("GET", "/api/channels/{channel_instance_id}/tasks"),
    # 交互记录只读：由 AgentRuntime 执行时产生，人工写入会污染审计与成本统计；
    # 删除走 worker 的保留期巡检，不给手动入口。
    ("GET", "/api/channels/{channel_instance_id}/interactions"),
    ("GET", "/api/tasks/{task_id}/interactions"),
    ("GET", "/api/interactions/{interaction_id}"),
    ("GET", "/api/health"),
    ("GET", "/api/tools"),
    ("POST", "/api/channels/{channel_instance_id}/memories"),
    ("POST", "/api/channels/{channel_instance_id}/tags"),
    ("PATCH", "/api/channels/{channel_instance_id}/tags/{tag_id}"),
    ("DELETE", "/api/channels/{channel_instance_id}/tags/{tag_id}"),
    ("PUT", "/api/channels/{channel_instance_id}/budget"),
    ("PUT", "/api/channels/{channel_instance_id}/policy"),
    # MCP server 管理（docs/SPEC-mcp-management.md）
    ("GET", "/api/channels/{channel_instance_id}/mcp-servers"),
    ("POST", "/api/channels/{channel_instance_id}/mcp-servers"),
    ("PUT", "/api/channels/{channel_instance_id}/mcp-servers/{server_id}"),
    ("DELETE", "/api/channels/{channel_instance_id}/mcp-servers/{server_id}"),
    ("POST", "/api/channels/{channel_instance_id}/mcp-servers/test"),
    # Skill 管理。两组作用域：全局库的 CRUD，与「某频道启用哪些」。
    # skill 是本控制台里唯一的全局资源，故这四条不带 channel 前缀。
    ("GET", "/api/skills"),
    ("POST", "/api/skills"),
    ("PUT", "/api/skills/{skill_id}"),
    ("DELETE", "/api/skills/{skill_id}"),
    ("GET", "/api/channels/{channel_instance_id}/skills"),
    ("PUT", "/api/channels/{channel_instance_id}/skills"),
    # 待审批列表。**只读** —— 放行必须回频道线程做，因为 Admin API 的 actor
    # 是前端随便填的，而审批的审计链不该建在不可信字段上（SPEC §6.4）。
    ("GET", "/api/channels/{channel_instance_id}/approvals"),
    # 附带文件挂在 skill 下：一个文件脱离它的 skill 没有意义，
    # 且 path 的唯一性是「同一 skill 内」而非全局。
    ("GET", "/api/skills/{skill_id}/files/{file_id}"),
    ("POST", "/api/skills/{skill_id}/files"),
    ("PUT", "/api/skills/{skill_id}/files/{file_id}"),
    ("DELETE", "/api/skills/{skill_id}/files/{file_id}"),
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
