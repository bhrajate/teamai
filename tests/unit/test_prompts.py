"""系统提示词组装。

重点防一类沉默失效：标签字段在模型、DB、序列化里都有，却没被拼进提示词。
`output_style` 曾经就是这样 —— 建标签时能填、API 能读回、控制台有输入框，
但提示词里从来没有它，填了完全没效果，且不报任何错。

故这里有一条按 dataclass 字段反查的用例：TagTemplate 上新增字段而忘了接进
提示词时，它会直接失败，而不是等到有人发现「填了没用」。
"""

from __future__ import annotations

import dataclasses

from teamai.application.agent.prompts import build_system_prompt
from teamai.domain.models import ChannelInstance, PermissionPolicy, TagTemplate

CH = ChannelInstance(
    id="ch_1",
    platform="slack",
    channel_id="C1",
    workspace_id="W1",
    agent_identity="ai_1",
)

POLICY = PermissionPolicy(id="pol_1", channel_instance_id="ch_1", allowed_tools=["github", "crm"])


def test_带上频道身份与平台() -> None:
    out = build_system_prompt(CH, POLICY)
    assert "ch_1" in out
    assert "slack" in out


def test_列出授权工具() -> None:
    out = build_system_prompt(CH, POLICY)
    assert "github" in out
    assert "crm" in out


def test_无策略时明示无工具授权() -> None:
    """不能留空 —— 模型看到空的工具列表可能以为「随便用」。"""
    out = build_system_prompt(CH, None)
    assert "无工具授权" in out


def test_未授权的工具不出现() -> None:
    out = build_system_prompt(CH, POLICY)
    assert "monitoring" not in out


def test_三个标签字段都进提示词() -> None:
    out = build_system_prompt(
        CH,
        POLICY,
        role="资深后端工程师",
        tag_instruction="逐文件审查改动",
        output_style="要点式，每条一行",
    )
    assert "资深后端工程师" in out
    assert "逐文件审查改动" in out
    assert "要点式，每条一行" in out


def test_风格排在指令之后() -> None:
    """风格约束产出形式，与指令冲突时应由它决定最终呈现，故须在后。"""
    out = build_system_prompt(CH, POLICY, tag_instruction="指令内容", output_style="风格内容")
    assert out.index("风格内容") > out.index("指令内容")


def test_缺省的标签字段不留空段() -> None:
    """None 的字段不该在提示词里留下「角色设定：」这类空标题。"""
    out = build_system_prompt(CH, POLICY)
    assert "角色设定" not in out
    assert "预设指令" not in out
    assert "输出风格" not in out


def test_额外上下文单独成段() -> None:
    out = build_system_prompt(CH, POLICY, extra_context="上季度已迁到 k8s")
    assert "上季度已迁到 k8s" in out


def test_标签的每个内容字段都被用到() -> None:
    """按 TagTemplate 的字段反查，防新增字段漏接。

    只查「内容型」字段：id / channel_instance_id 这些是标识与关联，
    本就不该进提示词；name 是激活时的引用名，也不必进。
    """
    skip = {
        "id",
        "channel_instance_id",
        "name",  # 激活用的引用名，不是给模型看的内容
        "shared",
        "active",
        "created_by",
        "created_at",
    }
    content_fields = [f.name for f in dataclasses.fields(TagTemplate) if f.name not in skip]

    # 每个内容字段填一个可识别的值，然后要求它出现在提示词里
    values = {name: f"<<{name}>>" for name in content_fields}
    tag = TagTemplate(
        id="tag_1",
        channel_instance_id="ch_1",
        name="t",
        instruction=values.get("instruction", ""),
        role=values.get("role"),
        output_style=values.get("output_style"),
    )

    out = build_system_prompt(
        CH,
        POLICY,
        role=tag.role,
        tag_instruction=tag.instruction,
        output_style=tag.output_style,
    )

    missing = [name for name in content_fields if values[name] not in out]
    assert not missing, (
        f"TagTemplate 的这些字段没被拼进系统提示词：{missing}。"
        "\n它们在控制台上可填、API 能读回，但对模型完全没有效果 —— "
        "要么接进 prompts.py，要么从模型里删掉。"
    )
