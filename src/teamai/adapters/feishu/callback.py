"""飞书 HTTP 回调：FastAPI 路由内的解密、校验、分发。

不用 lark-oapi 的 `dispatcher.do()`：自行完成解密 → token 校验 → challenge
直返 → 验签 → 去重 → 分发，全程 async、无线程边界，与 Slack Events API 路径
对称。绕开 SDK 的代价是自行实现 AES 解密与签名校验（crypto.py）。

处理时序（§6.3）：
  body 含 "encrypt" → 解密；token 不等 → 401；url_verification → 直接回
  challenge（必须早于验签，该请求不带签名头）；验签失败 → 401；去重命中 →
  200 忽略；事件进后台任务后立即回 200 —— 飞书与 Slack 一样要求快速回 200，
  超时即重投。

bot_open_id 在启动时拉取（连接器 startup）后回填到本 handler，事件翻译的
mention 判定依赖它。
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable, Coroutine
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse, Response
from lark_oapi.event.dispatcher_handler import P2ImMessageReceiveV1

from teamai.adapters.feishu.crypto import decrypt, verify_sign
from teamai.adapters.feishu.translator import dedup_key, to_incoming
from teamai.application.events import IncomingMessage
from teamai.domain.ports import EventDeduplicator

logger = logging.getLogger(__name__)


class FeishuCallbackHandler:
    """`POST /feishu/events` 的处理逻辑（回调模式）。

    dispatch 由连接器提供（路由 + 回复），本 handler 只做 HTTP 校验与翻译；
    翻译在后台任务里进行，校验失败一律即时回非 200。
    """

    def __init__(
        self,
        dispatch: Callable[[IncomingMessage], Coroutine[Any, Any, None]],
        dedup: EventDeduplicator,
        *,
        encrypt_key: str,
        verification_token: str,
        bot_open_id: str = "",
    ) -> None:
        self._dispatch = dispatch
        self._dedup = dedup
        self._encrypt_key = encrypt_key
        self._verification_token = verification_token
        # 连接器 startup 时拉取 bot 身份后回填
        self.bot_open_id = bot_open_id
        # 派发中的后台任务。事件循环对 task 只持弱引用，不在这里存一份强引用，
        # 任务可能在执行到一半时被 GC 掉，消息静默消失（asyncio 官方文档明示）。
        self._pending: set[asyncio.Task[None]] = set()

    async def handle(self, request: Request) -> Response:
        body_bytes = await request.body()
        raw_body = body_bytes.decode("utf-8")
        try:
            payload: dict = json.loads(raw_body)
        except ValueError:
            return JSONResponse(status_code=400, content={"code": 400, "msg": "invalid json"})

        # ① Encrypt Key 解密
        if payload.get("encrypt"):
            try:
                payload = json.loads(decrypt(self._encrypt_key, str(payload["encrypt"])))
            except Exception as exc:  # noqa: BLE001 - 解密失败一律拒
                logger.warning(f"飞书回调解密失败: {exc}")
                return JSONResponse(status_code=401, content={"code": 401, "msg": "decrypt failed"})

        # ② verification_token 比对。p2 事件在 header.token，url_verification 在顶层 token
        header = payload.get("header")
        token = header.get("token") if isinstance(header, dict) else payload.get("token")
        if not self._verification_token or token != self._verification_token:
            return JSONResponse(status_code=401, content={"code": 401, "msg": "invalid token"})

        # ③ URL 验证挑战：必须早于验签（url_verification 请求不带签名头）
        if payload.get("type") == "url_verification":
            return JSONResponse(content={"challenge": payload.get("challenge")})

        # ④ 签名校验（与 SDK 的 _verify_sign 一致：未配 encrypt_key 时不验签）
        if self._encrypt_key:
            signature = request.headers.get("X-Lark-Signature", "")
            timestamp = request.headers.get("X-Lark-Request-Timestamp", "")
            nonce = request.headers.get("X-Lark-Request-Nonce", "")
            if not verify_sign(timestamp, nonce, self._encrypt_key, raw_body, signature):
                return JSONResponse(status_code=401, content={"code": 401, "msg": "invalid signature"})

        # ⑤ 只处理 im.message.receive_v1，其余事件类型回 200 忽略
        if not isinstance(header, dict) or header.get("event_type") != "im.message.receive_v1":
            return JSONResponse(content={"code": 0, "msg": "success"})
        event_id = header.get("event_id")
        if not isinstance(event_id, str):
            return JSONResponse(content={"code": 0, "msg": "success"})

        # ⑥ 去重（键与长连接模式一致），命中即回 200 不派发
        key = dedup_key(event_id)
        if await self._dedup.is_duplicate(key):
            logger.info(f"忽略重投的飞书事件: {key}")
            return JSONResponse(content={"code": 0, "msg": "success"})

        # ⑦ 翻译 + 后台任务处理，立即回 200
        msg = to_incoming(P2ImMessageReceiveV1(payload), self.bot_open_id)
        if msg is not None:
            task = asyncio.create_task(self._dispatch(msg), name=f"feishu-dispatch-{key}")
            # 存住强引用直到跑完，见 _pending 的说明
            self._pending.add(task)
            task.add_done_callback(self._pending.discard)
        return JSONResponse(content={"code": 0, "msg": "success"})
