"""飞书 HTTP 回调路由测试。

固化时序结论：challenge 早于验签直返、token 不等拒、去重拦截重投、
非 im.message.receive_v1 回 200 忽略、正常事件派发后台任务。

直接构造 Request 调 handler（不走 TestClient）：handler 内的 create_task
落在测试自身的事件循环上，才能用 sleep(0) 等到派发的任务执行完。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from fastapi import Request

from teamai.adapters.feishu.callback import FeishuCallbackHandler
from teamai.application.events import IncomingMessage
from teamai.infrastructure.dedup import InMemoryEventDeduplicator

ENCRYPT_KEY = "test-encrypt-key"
VERIFICATION_TOKEN = "test-token"


def _sign(body: str, timestamp: str = "1710000000", nonce: str = "n1") -> str:
    return hashlib.sha256(
        (timestamp + nonce + ENCRYPT_KEY).encode("utf-8") + body.encode("utf-8")
    ).hexdigest()


def _encrypt(plaintext: str) -> str:
    """用 encrypt_key 加密出密文（crypto 正确性由 test_feishu_crypto 独立验证）。"""
    key = hashlib.sha256(ENCRYPT_KEY.encode()).digest()
    iv = os.urandom(16)
    padder = padding.PKCS7(128).padder()
    padded = padder.update(plaintext.encode()) + padder.finalize()
    ct = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    return base64.b64encode(iv + ct.update(padded) + ct.finalize()).decode()


class FakeDispatcher:
    def __init__(self) -> None:
        self.dispatched: list[IncomingMessage] = []

    async def __call__(self, msg: IncomingMessage) -> None:  # type: ignore[override]
        self.dispatched.append(msg)


def _event_body(
    *,
    event_id: str = "ev_1",
    event_type: str = "im.message.receive_v1",
    token: str = VERIFICATION_TOKEN,
) -> dict:
    return {
        "schema": "2.0",
        "header": {
            "event_id": event_id,
            "event_type": event_type,
            "tenant_key": "t_1",
            "app_id": "cli_1",
            "token": token,
        },
        "event": {
            "sender": {"sender_id": {"open_id": "ou_user1"}, "sender_type": "user"},
            "message": {
                "message_id": "om_1",
                "chat_id": "oc_1",
                "chat_type": "group",
                "message_type": "text",
                "content": json.dumps({"text": "你好"}),
                "create_time": 1700000000000,
            },
        },
    }


def _handler(dispatcher: FakeDispatcher, dedup: InMemoryEventDeduplicator) -> FeishuCallbackHandler:
    return FeishuCallbackHandler(
        dispatcher,
        dedup,
        encrypt_key=ENCRYPT_KEY,
        verification_token=VERIFICATION_TOKEN,
        bot_open_id="ou_bot123",
    )


def _request(raw_body: str, *, with_sign: bool = True, signature: str = "") -> Request:
    headers: list[tuple[bytes, bytes]] = [
        (b"content-type", b"application/json"),
        (b"x-lark-request-timestamp", b"1710000000"),
        (b"x-lark-request-nonce", b"n1"),
    ]
    if with_sign:
        headers.append((b"x-lark-signature", (signature or _sign(raw_body)).encode()))

    async def receive() -> dict:
        return {"type": "http.request", "body": raw_body.encode("utf-8"), "more_body": False}

    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/feishu/events",
            "headers": headers,
        },
        receive=receive,
    )


class TestFeishuCallback:
    async def test_url_verification_challenge直返(self) -> None:
        """challenge 必须早于验签：url_verification 请求不带签名头也能通过。"""
        dispatcher = FakeDispatcher()
        handler = _handler(dispatcher, InMemoryEventDeduplicator())
        body = {"type": "url_verification", "challenge": "ajls384kdjx98XX", "token": VERIFICATION_TOKEN}
        resp = await handler.handle(_request(json.dumps(body), with_sign=False))
        assert resp.status_code == 200
        assert resp.body == b'{"challenge":"ajls384kdjx98XX"}'
        assert dispatcher.dispatched == []

    async def test_token不匹配返回401(self) -> None:
        dispatcher = FakeDispatcher()
        handler = _handler(dispatcher, InMemoryEventDeduplicator())
        body = _event_body(token="wrong-token")
        resp = await handler.handle(_request(json.dumps(body)))
        assert resp.status_code == 401
        assert dispatcher.dispatched == []

    async def test_签名错误返回401(self) -> None:
        dispatcher = FakeDispatcher()
        handler = _handler(dispatcher, InMemoryEventDeduplicator())
        body = json.dumps(_event_body())
        resp = await handler.handle(_request(body, signature="0" * 64))
        assert resp.status_code == 401
        assert dispatcher.dispatched == []

    async def test_正常事件派发后台任务(self) -> None:
        dispatcher = FakeDispatcher()
        handler = _handler(dispatcher, InMemoryEventDeduplicator())
        body = json.dumps(_event_body())
        resp = await handler.handle(_request(body))
        assert resp.status_code == 200
        await asyncio.sleep(0)  # 让 create_task 派发的任务跑一轮
        assert len(dispatcher.dispatched) == 1
        assert dispatcher.dispatched[0].platform == "feishu"
        assert dispatcher.dispatched[0].event_id == "feishu:ev_1"

    async def test_重投事件被去重拦截(self) -> None:
        dispatcher = FakeDispatcher()
        handler = _handler(dispatcher, InMemoryEventDeduplicator())
        body = json.dumps(_event_body(event_id="ev_777"))
        first = await handler.handle(_request(body))
        second = await handler.handle(_request(body))
        assert first.status_code == 200 and second.status_code == 200
        await asyncio.sleep(0)
        assert len(dispatcher.dispatched) == 1, "重投不应再次派发"

    async def test_非receive_v1事件回200忽略(self) -> None:
        dispatcher = FakeDispatcher()
        handler = _handler(dispatcher, InMemoryEventDeduplicator())
        body = json.dumps(_event_body(event_type="im.message.message_read_v1"))
        resp = await handler.handle(_request(body))
        assert resp.status_code == 200
        await asyncio.sleep(0)
        assert dispatcher.dispatched == []

    async def test_加密body先解密再校验(self) -> None:
        """encrypt 包裹时：对密文整体解密后走同一套 token/challenge/验签流程。"""
        dispatcher = FakeDispatcher()
        handler = _handler(dispatcher, InMemoryEventDeduplicator())
        plaintext = json.dumps(_event_body())
        raw_body = json.dumps({"encrypt": _encrypt(plaintext)})
        resp = await handler.handle(_request(raw_body))
        assert resp.status_code == 200
        await asyncio.sleep(0)
        assert len(dispatcher.dispatched) == 1
