"""Admin API 访问令牌校验。

未配 `ADMIN_API_TOKEN` 时放行全部请求 —— 保持既有部署（内网、仅平台接入）
零改动可用。配了则资源路由一律要求 `Authorization: Bearer <token>`，
`/api/health` 不在保护范围内（探针与 make verify-* 要匿名可打）。

令牌比较走 `secrets.compare_digest`：普通 `==` 在首个不等字节即返回，
逐字节计时差可被用来逐位猜出令牌。
"""

from __future__ import annotations

import secrets

from fastapi import Header, HTTPException

from teamai.config import settings

__all__ = ["require_admin_token"]


async def require_admin_token(authorization: str | None = Header(default=None)) -> None:
    expected = settings.admin_api_token
    if not expected:
        return

    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not secrets.compare_digest(token, expected):
        # 401 而非 403：凭据缺失或不对，属未认证。带上 WWW-Authenticate 便于客户端识别。
        raise HTTPException(
            status_code=401,
            detail="Admin API 令牌无效",
            headers={"WWW-Authenticate": "Bearer"},
        )
