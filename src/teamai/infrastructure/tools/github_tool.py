"""GitHub 工具连接器（代码读取、issue 查询、PR 创建)。

使用 httpx 直连 GitHub REST API。未配置 token 时返回明确的配置错误。

工具以普通 async 函数形式声明，参数类型即契约：``action`` 是 ``Literal``，
非法取值由 pydantic-ai 在调用前拦下并把校验错误回灌给模型，不会进到函数体。
按 action 条件必填的参数（如 read_file 的 path）无法用 schema 表达，
在函数体里检查并抛 ``ModelRetry``，同样能让模型补齐参数后重试。
"""

from __future__ import annotations

import os
from typing import Literal

import httpx
from pydantic_ai import ModelRetry, Tool

from teamai.infrastructure.tools.base import check_http, fail, ok

_API = "https://api.github.com"
_TIMEOUT = 15


def _headers(token: str, accept: str = "application/vnd.github+json") -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "Accept": accept}


def _require(action: str, **params: str | None) -> None:
    """检查按 action 条件必填的参数。缺参可由模型自行补齐，故用 ModelRetry。"""
    missing = [name for name, value in params.items() if not value]
    if missing:
        raise ModelRetry(f"action={action} 缺少必填参数：{', '.join(missing)}。请补齐后重试。")


async def _read_file(token: str, repo: str, path: str | None) -> str:
    _require("read_file", path=path)
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.get(
            f"{_API}/repos/{repo}/contents/{path}",
            headers=_headers(token, "application/vnd.github.raw+json"),
        )
    check_http(resp, f"读取 {repo}/{path}")
    return ok(path=path, content=resp.text)


async def _list_issues(token: str, repo: str) -> str:
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.get(
            f"{_API}/repos/{repo}/issues",
            params={"state": "open", "per_page": 10},
            headers=_headers(token),
        )
    check_http(resp, f"查询 {repo} 的 issue")
    issues = [{"number": i["number"], "title": i["title"], "html_url": i["html_url"]} for i in resp.json()]
    return ok(issues=issues)


async def _create_pr(
    token: str,
    repo: str,
    *,
    title: str | None,
    body: str | None,
    head: str | None,
    base: str | None,
) -> str:
    _require("create_pr", title=title, head=head, base=base)
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.post(
            f"{_API}/repos/{repo}/pulls",
            headers=_headers(token),
            json={"title": title, "head": head, "base": base, "body": body or ""},
        )
    check_http(resp, f"在 {repo} 创建 PR")
    data = resp.json()
    return ok(pr_number=data["number"], html_url=data["html_url"])


def build_github_tool(token: str | None = None) -> Tool:
    resolved = token or os.environ.get("GITHUB_TOKEN", "")

    async def github(
        action: Literal["read_file", "list_issues", "create_pr"],
        repo: str,
        path: str | None = None,
        title: str | None = None,
        body: str | None = None,
        head: str | None = None,
        base: str | None = None,
    ) -> str:
        """访问 GitHub：读取代码文件、查询 open issue、创建 PR。

        Args:
            action: 要执行的操作，取 read_file、list_issues 或 create_pr。
            repo: 目标仓库，形如 owner/repo。
            path: 文件路径，action=read_file 时必填。
            title: PR 标题，action=create_pr 时必填。
            body: PR 描述，可选。
            head: PR 源分支，action=create_pr 时必填。
            base: PR 目标分支，action=create_pr 时必填。
        """
        if not resolved:
            return fail("GitHub 未配置访问令牌（GITHUB_TOKEN）")
        if action == "read_file":
            return await _read_file(resolved, repo, path)
        if action == "list_issues":
            return await _list_issues(resolved, repo)
        return await _create_pr(resolved, repo, title=title, body=body, head=head, base=base)

    return Tool(github)
