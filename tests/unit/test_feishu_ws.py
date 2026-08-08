"""飞书长连接桥接测试。

固化的核心不变量：SDK 在 WS 接收协程内**同步**调用 handler，因此 handler
必须 fire-and-forget —— 投递后立即返回，不阻塞调用方；处理在主 loop 执行。
"""

from __future__ import annotations

import asyncio

from lark_oapi.event.dispatcher_handler import P2ImMessageReceiveV1

from teamai.adapters.feishu.ws import FeishuWsSession


def _raw_event() -> P2ImMessageReceiveV1:
    return P2ImMessageReceiveV1(
        {
            "schema": "2.0",
            "header": {"event_id": "ev_1", "tenant_key": "t_1", "app_id": "cli_1"},
            "event": {
                "sender": {"sender_id": {"open_id": "ou_user1"}, "sender_type": "user"},
                "message": {
                    "message_id": "om_1",
                    "chat_id": "oc_1",
                    "chat_type": "group",
                    "message_type": "text",
                    "content": '{"text":"你好"}',
                    "create_time": 1700000000000,
                },
            },
        }
    )


async def test_同步handler_fire_and_forget不阻塞() -> None:
    """投递后立即返回，事件在目标 loop 上异步执行 —— 阻塞会卡死 _ping_loop。"""
    loop = asyncio.get_running_loop()
    received: list[P2ImMessageReceiveV1] = []

    async def handle(data: P2ImMessageReceiveV1) -> None:
        await asyncio.sleep(0.01)  # 模拟异步处理耗时
        received.append(data)

    session = FeishuWsSession(handle, loop, app_id="a", app_secret="s", domain="https://open.feishu.cn")
    # 不 start()（避免真连 WS），直接调同步桥 —— 这正是 SDK 调用 handler 的方式
    data = _raw_event()
    session._on_message(data)  # type: ignore[attr-defined]

    assert received == [], "同步调用必须立即返回，不得阻塞等待处理完成"
    await asyncio.wait_for(asyncio.sleep(0.05), timeout=1.0)
    assert len(received) == 1
    assert received[0] is data


async def test_桥接使用startup时传入的主loop() -> None:
    """run_coroutine_threadsafe 必须投到 uvicorn 的主 loop，不能在模块级取 loop。"""
    main_loop = asyncio.get_running_loop()
    got_loop: list[asyncio.AbstractEventLoop] = []

    async def handle(data: P2ImMessageReceiveV1) -> None:
        got_loop.append(asyncio.get_running_loop())

    session = FeishuWsSession(handle, main_loop, app_id="a", app_secret="s", domain="https://open.feishu.cn")
    session._on_message(_raw_event())  # type: ignore[attr-defined]
    await asyncio.sleep(0.05)
    assert got_loop == [main_loop]
