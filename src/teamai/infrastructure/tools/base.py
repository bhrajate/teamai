"""工具层共享约定。

工具一律写成带类型标注的普通 async 函数，交给 pydantic-ai 从签名生成
JSON Schema 并在调用前校验参数。这里只放三样共享的东西：

- ``ToolUnavailable``：工具不可用且重试无意义（缺凭据、缺实现），
  由注册侧转成一条错误文本交给模型，让它向用户说明而不是让整个任务失败。
- ``ok`` / ``fail``：统一把结果序列化成 JSON 文本（模型读到的是文本，
  用 ``str(dict)`` 会得到单引号的 Python 字面量，不利于模型解析）。
- ``check_http``：把 HTTP 状态码分流成「模型可自救」与「模型救不了」两类。

可重试的失败抛 ``pydantic_ai.ModelRetry``，错误信息会回灌给模型让它改参数重试；
不可重试的抛 ``ToolUnavailable``。业务成功走 ``ok(...)``。
"""

from __future__ import annotations

import json
from typing import Any

import httpx
from pydantic_ai import ModelRetry


class ToolUnavailable(Exception):
    """工具无法执行，且模型重试也无意义（如未配置凭据、集成点未实现）。"""


def ok(**data: Any) -> str:
    """成功结果。序列化为 JSON 文本，非 JSON 原生类型退化为 str。"""
    return json.dumps({"ok": True, **data}, ensure_ascii=False, default=str)


def fail(error: str) -> str:
    """失败结果。用于不值得重试、但要让模型知情的情况。"""
    return json.dumps({"ok": False, "error": error}, ensure_ascii=False)


def check_http(resp: httpx.Response, what: str) -> None:
    """按状态码分流。

    - 2xx：直接返回
    - 401/403：凭据问题，模型改参数也没用 → ``ToolUnavailable``
    - 其余：可能是路径/参数写错或对端抖动 → ``ModelRetry``，让模型带着
      错误信息重试一次
    """
    if resp.is_success:
        return
    if resp.status_code in (401, 403):
        raise ToolUnavailable(f"{what}失败：凭据无效或权限不足（HTTP {resp.status_code}）")
    raise ModelRetry(f"{what}失败：HTTP {resp.status_code} {resp.text[:200]}")
