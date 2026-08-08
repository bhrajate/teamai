"""规范入向事件：平台无关的消息信封。

各平台 translator 把各自的 webhook 事件归一成 IncomingMessage，router 之后
不再感知平台。字段名刻意取中性（thread_ref 而非 thread_ts），取值规则由各
平台 translator 决定。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class IncomingMessage:
    platform: str  # "slack" | "feishu"
    event_id: str  # 去重用，已含平台前缀（如 "slack:Ev123" / "feishu:om_xx"）
    workspace_id: str  # slack: team        feishu: header.tenant_key
    channel_id: str  # slack: channel     feishu: chat_id
    channel_type: str  # slack: channel/im/mpim   feishu: group/p2p
    user_id: str  # slack: user        feishu: sender.sender_id.open_id
    text: str  # 已剥离 @提及、已从 content JSON 取出的纯文本
    message_id: str  # 本条消息自身 ID。slack: ts   feishu: message_id
    thread_ref: str  # 线程根引用，回复时用
    is_mention: bool  # 飞书由 mentions[] 比对 bot open_id 得出
    raw: dict[str, Any] = field(default_factory=dict)  # 逃生舱，router 不读
