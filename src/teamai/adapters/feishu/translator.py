"""飞书事件 → IncomingMessage 翻译。

飞书与 Slack 的三个结构性差异在此消化：
① `message.content` 是 JSON 字符串，需二次 parse 取 `{"text": "..."}`；
② 群聊里所有消息都推过来，是否 @ 了 bot 只能靠遍历 `mentions[]` 比对
   bot 自身的 `open_id`（Slack 侧 slack-bolt 已用 app_mention 事件类型代劳）；
③ content 内只有占位符 `@_user_1`，真身在 `mentions[]`，回复前须换回真名。

bot_open_id 事件里没有，须由连接器启动时调 `/open-apis/bot/v3/info` 取一次
传入 —— `is_mention` 完全依赖它。
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from teamai.application.events import IncomingMessage

if TYPE_CHECKING:
    from lark_oapi.event.dispatcher_handler import P2ImMessageReceiveV1


def dedup_key(event_id: str) -> str:
    """去重键：带 `feishu:` 前缀与 Slack 的 ID 空间隔离（规则见 slack/translator.py）。"""
    return f"feishu:{event_id}"


def to_incoming(data: P2ImMessageReceiveV1, bot_open_id: str) -> IncomingMessage | None:
    """把 im.message.receive_v1 事件归一成 IncomingMessage。

    非文本消息返回 None（本设计只处理 `message_type == "text"`）。
    """
    msg = data.event.message
    if msg.message_type != "text":
        return None

    # ① content 是 JSON 字符串，取 text 字段
    text = json.loads(msg.content or "{}").get("text", "")

    # ② 遍历 mentions[] 比对 bot 自身 open_id；p2p 单聊无需 @ 一律视作 mention
    mentions = msg.mentions or []
    is_mention = any(m.id is not None and m.id.open_id == bot_open_id for m in mentions)
    if msg.chat_type == "p2p":
        is_mention = True

    # ③ 占位符换成真名：@ bot 自身的占位符剥掉（否则 prompt 里出现 @_user_1）
    for m in mentions:
        key = m.key or ""
        if not key:
            continue
        if m.id is not None and m.id.open_id == bot_open_id:
            text = text.replace(key, "")
        else:
            text = text.replace(key, f"@{m.name}")

    return IncomingMessage(
        platform="feishu",
        event_id=dedup_key(data.header.event_id),
        workspace_id=data.header.tenant_key,
        channel_id=msg.chat_id,
        channel_type=msg.chat_type,
        user_id=data.event.sender.sender_id.open_id,
        text=text.strip(),
        message_id=msg.message_id,
        thread_ref=msg.root_id or msg.message_id,
        is_mention=is_mention,
    )
