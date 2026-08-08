"""Slack 事件 → IncomingMessage 翻译与去重键。"""

from __future__ import annotations

from teamai.application.events import IncomingMessage


def dedup_key(body: dict) -> str:
    """从 Slack 请求信封取去重键，并带 `slack:` 前缀与飞书隔离。

    优先用信封里的 `event_id`：Slack 为每个事件分配一次，重投时保持不变，
    是官方指定的去重依据（Events API 与 Socket Mode 的 body 都带它）。

    取不到才退回 `channel:ts:subtype` 拼装 —— 这个组合能标识「哪条消息」，
    但同一条消息的多次重投也只会得到同一个键，故仍可用；只是遇上编辑消息
    等 ts 相同而内容不同的场景会误判，所以只作兜底。

    前缀让两平台各自命名空间内的键不会互相误判：飞书侧同样加 `feishu:`。
    """
    event_id = str(body.get("event_id", ""))
    if event_id:
        return f"slack:{event_id}"
    event = body.get("event", {}) or {}
    return f"slack:{event.get('channel', '')}:{event.get('ts', '')}:{event.get('subtype', '')}"


def event_to_incoming(event: dict, body: dict, *, is_mention: bool) -> IncomingMessage:
    """把 Slack 事件归一成 IncomingMessage。

    thread_ref 取 `thread_ts or ts`：已在线程内的消息回到线程根，独立消息
    以自身为根 —— 两种情况下 reply 都能正确落到目标线程。
    """
    ts = event.get("ts", "")
    return IncomingMessage(
        platform="slack",
        event_id=dedup_key(body),
        workspace_id=event.get("team", ""),
        channel_id=event.get("channel", ""),
        channel_type=event.get("channel_type", ""),
        user_id=event.get("user", ""),
        text=event.get("text", ""),
        message_id=ts,
        thread_ref=event.get("thread_ts") or ts,
        is_mention=is_mention,
        raw=event,
    )
