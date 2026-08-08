"""飞书事件翻译：content JSON 解析、mention 判定、占位符替换、thread_ref 取值。"""

from __future__ import annotations

from lark_oapi.event.dispatcher_handler import P2ImMessageReceiveV1

from teamai.adapters.feishu.translator import to_incoming

BOT_OPEN_ID = "ou_bot123"


def _event(
    *,
    message_type: str = "text",
    content: str = '{"text":"你好"}',
    chat_type: str = "group",
    chat_id: str = "oc_1",
    message_id: str = "om_1",
    root_id: str | None = None,
    mentions: list[dict] | None = None,
    sender_open_id: str = "ou_user1",
) -> P2ImMessageReceiveV1:
    message: dict = {
        "message_id": message_id,
        "chat_id": chat_id,
        "chat_type": chat_type,
        "message_type": message_type,
        "content": content,
        "create_time": 1700000000000,
    }
    if root_id is not None:
        message["root_id"] = root_id
    if mentions is not None:
        message["mentions"] = mentions
    return P2ImMessageReceiveV1(
        {
            "schema": "2.0",
            "header": {"event_id": "ev_1", "tenant_key": "t_1", "app_id": "cli_1"},
            "event": {
                "sender": {"sender_id": {"open_id": sender_open_id}, "sender_type": "user"},
                "message": message,
            },
        }
    )


class TestToIncoming:
    def test_content_json解析出纯文本(self) -> None:
        msg = to_incoming(_event(content='{"text":"帮我看下这段代码"}'), BOT_OPEN_ID)
        assert msg is not None
        assert msg.text == "帮我看下这段代码"
        assert msg.platform == "feishu"
        assert msg.workspace_id == "t_1"
        assert msg.channel_id == "oc_1"
        assert msg.channel_type == "group"
        assert msg.user_id == "ou_user1"
        assert msg.message_id == "om_1"
        assert msg.event_id == "feishu:ev_1"

    def test_群聊中未at_bot不算mention(self) -> None:
        msg = to_incoming(_event(content='{"text":"大家好"}', mentions=[]), BOT_OPEN_ID)
        assert msg is not None
        assert msg.is_mention is False

    def test_群聊中at了bot算mention(self) -> None:
        mentions = [{"key": "@_user_1", "id": {"open_id": BOT_OPEN_ID}, "name": "TeamAI"}]
        msg = to_incoming(_event(content='{"text":"@_user_1 帮我查下"}', mentions=mentions), BOT_OPEN_ID)
        assert msg is not None
        assert msg.is_mention is True

    def test_at_bot自身的占位符被剥掉(self) -> None:
        mentions = [{"key": "@_user_1", "id": {"open_id": BOT_OPEN_ID}, "name": "TeamAI"}]
        msg = to_incoming(_event(content='{"text":"@_user_1 帮我查下"}', mentions=mentions), BOT_OPEN_ID)
        assert msg is not None
        assert msg.text == "帮我查下"

    def test_at_其他人的占位符换回真名(self) -> None:
        mentions = [{"key": "@_user_1", "id": {"open_id": "ou_zhang"}, "name": "张三"}]
        msg = to_incoming(_event(content='{"text":"@_user_1 开会"}', mentions=mentions), BOT_OPEN_ID)
        assert msg is not None
        assert msg.text == "@张三 开会"

    def test_p2p单聊无需at一律视作mention(self) -> None:
        msg = to_incoming(_event(chat_type="p2p", content='{"text":"在吗"}'), BOT_OPEN_ID)
        assert msg is not None
        assert msg.is_mention is True

    def test_非文本消息返回None(self) -> None:
        msg = to_incoming(_event(message_type="image", content='{"image_key":"img_1"}'), BOT_OPEN_ID)
        assert msg is None

    def test_thread_ref取root_id优先(self) -> None:
        msg = to_incoming(_event(root_id="om_root", message_id="om_child"), BOT_OPEN_ID)
        assert msg is not None
        assert msg.thread_ref == "om_root"

    def test_thread_ref无root时取自身message_id(self) -> None:
        msg = to_incoming(_event(message_id="om_solo"), BOT_OPEN_ID)
        assert msg is not None
        assert msg.thread_ref == "om_solo"

    def test_空content不抛(self) -> None:
        msg = to_incoming(_event(content=""), BOT_OPEN_ID)
        assert msg is not None
        assert msg.text == ""
