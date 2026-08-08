"""飞书出向消息发送（MessagePublisher 实现）。

回复用 `im/v1/messages/{message_id}/reply`（SDK 的 areply）：thread_ref 即
根消息的 message_id（飞书 reply 接口接受任意 message_id 并就地成串），
与 Slack 的 thread_ts 语义对齐。
"""

from __future__ import annotations

import json
import logging

import lark_oapi
from lark_oapi.api.im.v1.model.reply_message_request import ReplyMessageRequest
from lark_oapi.api.im.v1.model.reply_message_request_body import ReplyMessageRequestBody

from teamai.config import settings
from teamai.domain.ports import MessagePublisher, ReplyTarget

logger = logging.getLogger(__name__)


def build_lark_client() -> lark_oapi.Client:
    domain = lark_oapi.FEISHU_DOMAIN if settings.platforms_feishu_domain == "feishu" else lark_oapi.LARK_DOMAIN
    return (
        lark_oapi.Client.builder()
        .app_id(settings.feishu_app_id)
        .app_secret(settings.feishu_app_secret)
        .domain(domain)
        .build()
    )


class FeishuPublisher(MessagePublisher):
    def __init__(self, client: lark_oapi.Client | None = None) -> None:
        self._client = client or build_lark_client()

    async def reply(self, target: ReplyTarget, text: str) -> None:
        request = (
            ReplyMessageRequest.builder()
            .message_id(target.thread_ref)
            .request_body(
                ReplyMessageRequestBody.builder()
                .msg_type("text")
                .content(json.dumps({"text": text}, ensure_ascii=False))
                .build()
            )
            .build()
        )
        resp = await self._client.im.v1.message.areply(request)
        if resp.code != 0:
            raise ConnectionError(f"飞书回复失败: {resp.code} {resp.msg}")

    async def aclose(self) -> None:
        """lark SDK 的异步请求每发一次自建并关闭 httpx 连接（Transport.aexecute
        源码），无长期存活的连接池，故无需收尾。"""
        return None
