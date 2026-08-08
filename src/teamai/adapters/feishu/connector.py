"""飞书连接器：回调 / 长连接两种接入方式收进统一生命周期。

接入方式由 `platforms.feishu.mode` 决定：callback / ws / auto。
auto 的推断规则：配了 encrypt_key + verification_token 走 callback（需要
HTTP 回调 URL），否则走 ws（长连接，无需公网入口）。两种模式所需凭据有重叠，
故给显式开关而非像 Slack 那样按凭据隐式推断。

两模式共用的动作：启动时拉取 bot 自身 open_id（`is_mention` 判定依赖，
事件里没有该信息），路由与回复经 `dispatch()`。
"""

from __future__ import annotations

import asyncio
import logging

import lark_oapi
from fastapi import FastAPI, Request, Response
from lark_oapi.channel.bot_identity import fetch_bot_identity
from lark_oapi.event.dispatcher_handler import P2ImMessageReceiveV1

from teamai.adapters.base import PlatformConnector
from teamai.adapters.feishu.callback import FeishuCallbackHandler
from teamai.adapters.feishu.translator import to_incoming
from teamai.adapters.feishu.ws import FeishuWsSession
from teamai.application.events import IncomingMessage
from teamai.application.router import MessageRouter
from teamai.config import settings
from teamai.domain.ports import EventDeduplicator, MessagePublisher, ReplyTarget

logger = logging.getLogger(__name__)


def _feishu_domain() -> str:
    return lark_oapi.FEISHU_DOMAIN if settings.platforms_feishu_domain == "feishu" else lark_oapi.LARK_DOMAIN


class FeishuConnector(PlatformConnector):
    name = "feishu"

    def __init__(
        self,
        router: MessageRouter,
        dedup: EventDeduplicator,
        publisher: MessagePublisher,
    ) -> None:
        self._router = router
        self._dedup = dedup
        self._publisher = publisher
        self._bot_open_id: str | None = None
        self._callback_handler: FeishuCallbackHandler | None = None
        self._ws_session: FeishuWsSession | None = None

    def _mode(self) -> str:
        cfg = settings.platforms_feishu_mode
        if cfg in ("callback", "ws"):
            return cfg
        if settings.feishu_encrypt_key and settings.feishu_verification_token:
            return "callback"
        return "ws"

    def mount(self, app: FastAPI) -> None:
        """回调模式挂 `POST /feishu/events`；长连接模式为 no-op。"""
        if self._mode() != "callback":
            return
        self._callback_handler = FeishuCallbackHandler(
            self.dispatch,
            self._dedup,
            encrypt_key=settings.feishu_encrypt_key,
            verification_token=settings.feishu_verification_token,
        )
        handler = self._callback_handler
        assert handler is not None  # 刚赋值，闭包里保证非空

        @app.post("/feishu/events")
        async def feishu_events(request: Request) -> Response:
            return await handler.handle(request)

    async def startup(self) -> None:
        """拉取 bot 身份（mention 判定依赖），并按模式建立长连接。"""
        self._bot_open_id = await self._fetch_bot_open_id()
        if self._callback_handler is not None:
            self._callback_handler.bot_open_id = self._bot_open_id

        if self._mode() == "ws":
            loop = asyncio.get_running_loop()  # 必须是 uvicorn 的 loop，不能模块级取
            self._ws_session = FeishuWsSession(
                self._handle_ws_event,
                loop,
                app_id=settings.feishu_app_id,
                app_secret=settings.feishu_app_secret,
                domain=_feishu_domain(),
            )
            assert self._ws_session is not None  # 刚赋值
            self._ws_session.start()

    async def shutdown(self) -> None:
        """ws.Client 无干净的停止接口，daemon 线程随进程退出回收（已知取舍）。

        callback 模式无长期资源：api client 的异步请求每发一次自建并关闭
        httpx 连接（SDK Transport.aexecute 源码），无需收尾。
        """
        if self._ws_session is not None:
            logger.info("飞书长连接随进程退出回收（daemon 线程）")

    # ---------- 事件处理 ----------

    async def dispatch(self, msg: IncomingMessage) -> None:
        """路由 + 按需回复。回调与长连接两模式共用（异常在此兜住）。

        只有 @ 了 bot 才回复：observe 路径的「已记录频道上下文」不该打扰群聊。
        """
        try:
            decision = await self._router.route(msg)
            if msg.is_mention and decision.message:
                await self._publisher.reply(
                    ReplyTarget(
                        platform="feishu",
                        channel_id=msg.channel_id,
                        thread_ref=msg.thread_ref,
                    ),
                    decision.message,
                )
        except Exception as exc:
            logger.error(f"飞书消息处理失败: {exc}")

    async def _handle_ws_event(self, data: P2ImMessageReceiveV1) -> None:
        """长连接路径：去重 + 翻译 + dispatch。由 _on_message 投递到主 loop。"""
        try:
            msg = to_incoming(data, self._bot_open_id or "")
            if msg is None:
                return
            if await self._dedup.is_duplicate(msg.event_id):
                logger.info(f"忽略重投的飞书事件: {msg.event_id}")
                return
            await self.dispatch(msg)
        except Exception as exc:
            logger.error(f"飞书长连接消息处理失败: {exc}")

    # ---------- 内部 ----------

    async def _fetch_bot_open_id(self) -> str | None:
        """调 /open-apis/bot/v3/info 取 bot 自身 open_id（per-app，须每次启动取）。

        拉取失败只告警不中断启动：宁缺 mention 判定也不让进程起不来。
        """
        try:
            client = lark_oapi.Client.builder() \
                .app_id(settings.feishu_app_id) \
                .app_secret(settings.feishu_app_secret) \
                .domain(_feishu_domain()) \
                .build()
            identity = await fetch_bot_identity(client.config)
            if identity is not None:
                logger.info(f"飞书 bot open_id: {identity.open_id}")
                return identity.open_id
            logger.warning("飞书 bot 身份拉取失败，mention 判定将不可用")
        except Exception as exc:
            logger.warning(f"飞书 bot 身份拉取异常: {exc}")
        return None
