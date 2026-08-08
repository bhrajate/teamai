"""飞书长连接模式：ws.Client 独立线程 + 跨 loop 桥接。

lark-oapi 的 ws.Client 有两个硬约束（SDK 源码结论，升级版本需重新核对）：
- 模块导入时即抓一个全局 loop，`start()` 走 `loop.run_until_complete(...)`
  永久阻塞，不能在 uvicorn 的 loop 里用；
- WS 收帧后在 `_handle_data_frame` 协程内**同步调用**事件 handler。

故放独立 daemon 线程，handler 内只能 fire-and-forget 把处理投递到主 loop：
阻塞等待会卡死同线程的 `_ping_loop`，120 秒收不到 ping 即被服务端断连。

`ws.Client` 未暴露干净的停止接口（无 public close），`shutdown()` 只能依赖
daemon 线程随进程退出回收；这是已知取舍，生产建议用 callback 模式，
ws 定位为内网/开发场景。
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Callable, Coroutine
from typing import Any

import lark_oapi
from lark_oapi.event.dispatcher_handler import P2ImMessageReceiveV1

logger = logging.getLogger(__name__)


class FeishuWsSession:
    """一条飞书长连接：线程 + 事件桥接。

    长连接模式免校验（encrypt_key / verification_token 可传空串），
    去重与翻译在投递后的 async 任务里完成，语义与回调模式一致。
    """

    def __init__(
        self,
        handle: Callable[[P2ImMessageReceiveV1], Coroutine[Any, Any, None]],
        loop: asyncio.AbstractEventLoop,
        *,
        app_id: str,
        app_secret: str,
        domain: str,
    ) -> None:
        self._handle = handle
        self._loop = loop
        self._app_id = app_id
        self._app_secret = app_secret
        self._domain = domain
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        handler = (
            lark_oapi.EventDispatcherHandler.builder("", "")  # 长连接免校验，可传空串
            .register_p2_im_message_receive_v1(self._on_message)
            .build()
        )
        client = lark_oapi.ws.Client(
            self._app_id,
            self._app_secret,
            event_handler=handler,
            domain=self._domain,
        )
        self._thread = threading.Thread(target=client.start, daemon=True, name="feishu-ws")
        self._thread.start()
        logger.info("飞书长连接已建立")

    def _on_message(self, data: P2ImMessageReceiveV1) -> None:
        """SDK 在 WS 接收协程内同步调用。绝不可阻塞。

        刻意不 .result()：等待会把处理时间算进本帧接收，卡死同线程的
        _ping_loop，120s 无 ping 即被断连。异常由 handle 内部兜住，
        否则 future 被丢弃后异常静默消失。
        """
        asyncio.run_coroutine_threadsafe(self._handle(data), self._loop)
