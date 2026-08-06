"""GitHub 工具连接器（代码读取、issue 查询、PR 创建）。

使用 httpx 直连 GitHub REST API。未配置 token 时返回明确的配置错误。
"""

from __future__ import annotations

import os
from typing import Any

from teamai.tools.base import BaseTool, ToolError, ToolResult


class GitHubTool(BaseTool):
    name = "github"
    description = "访问 GitHub：读取代码文件、查询 issue、创建 PR。"
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["read_file", "list_issues", "create_pr"]},
            "repo": {"type": "string", "description": "仓库名，如 owner/repo"},
            "path": {"type": "string", "description": "文件路径（read_file 使用）"},
            "title": {"type": "string", "description": "PR 标题（create_pr 使用）"},
            "body": {"type": "string", "description": "PR 描述（create_pr 使用）"},
            "head": {"type": "string", "description": "源分支（create_pr 使用）"},
            "base": {"type": "string", "description": "目标分支（create_pr 使用）"},
        },
        "required": ["action"],
    }

    def __init__(self, token: str | None = None) -> None:
        self._token = token or os.environ.get("GITHUB_TOKEN", "")

    async def call(self, args: dict[str, Any], auth_scope: object) -> ToolResult:
        action = args.get("action")
        if not self._token:
            raise ToolError("GitHub 未配置访问令牌（GITHUB_TOKEN）")
        repo = args.get("repo", "")
        if not repo:
            raise ToolError("缺少 repo 参数")
        if action == "read_file":
            return await self._read_file(repo, args.get("path", ""))
        if action == "list_issues":
            return await self._list_issues(repo)
        if action == "create_pr":
            return await self._create_pr(repo, args)
        raise ToolError(f"不支持的 GitHub 操作: {action}")

    async def _read_file(self, repo: str, path: str) -> ToolResult:
        if not path:
            raise ToolError("read_file 需要 path 参数")
        import httpx

        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"https://api.github.com/repos/{repo}/contents/{path}",
                headers={"Authorization": f"Bearer {self._token}", "Accept": "application/vnd.github.raw+json"},
            )
        if resp.status_code != 200:
            raise ToolError(f"读取文件失败: HTTP {resp.status_code}")
        return ToolResult(ok=True, data={"path": path, "content": resp.text})

    async def _list_issues(self, repo: str) -> ToolResult:
        import httpx

        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"https://api.github.com/repos/{repo}/issues?state=open&per_page=10",
                headers={"Authorization": f"Bearer {self._token}", "Accept": "application/vnd.github+json"},
            )
        if resp.status_code != 200:
            raise ToolError(f"查询 issue 失败: HTTP {resp.status_code}")
        issues = [
            {"number": i["number"], "title": i["title"], "html_url": i["html_url"]}
            for i in resp.json()
        ]
        return ToolResult(ok=True, data={"issues": issues})

    async def _create_pr(self, repo: str, args: dict[str, Any]) -> ToolResult:
        import httpx

        required = ["title", "head", "base"]
        for key in required:
            if not args.get(key):
                raise ToolError(f"create_pr 需要 {key} 参数")
        payload = {"title": args["title"], "head": args["head"], "base": args["base"], "body": args.get("body", "")}
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"https://api.github.com/repos/{repo}/pulls",
                headers={"Authorization": f"Bearer {self._token}", "Accept": "application/vnd.github+json"},
                json=payload,
            )
        if resp.status_code not in (200, 201):
            raise ToolError(f"创建 PR 失败: HTTP {resp.status_code}: {resp.text[:200]}")
        data = resp.json()
        return ToolResult(ok=True, data={"pr_number": data["number"], "html_url": data["html_url"]})


def build_github_tool() -> GitHubTool:
    return GitHubTool()
