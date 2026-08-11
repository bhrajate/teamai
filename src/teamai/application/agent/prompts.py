"""系统提示词模板。"""

from __future__ import annotations

from teamai.domain.models import ChannelInstance, PermissionPolicy


def build_system_prompt(
    instance: ChannelInstance,
    policy: PermissionPolicy | None,
    *,
    role: str | None = None,
    tag_instruction: str | None = None,
    output_style: str | None = None,
    extra_context: str = "",
) -> str:
    """组装 Agent 系统提示词。

    包含：频道身份、可用工具、记忆引用规范、标签的角色/指令/输出风格。

    三项标签字段的顺序有意如此：角色定「以谁的身份」，指令定「做什么」，
    风格定「怎么写出来」。风格放在最后，因为它约束的是产出形式，
    与前两项冲突时应当由它决定最终呈现。
    """
    allowed = ", ".join(policy.allowed_tools) if policy else "（无工具授权）"

    prompt = f"""你是一个团队协作 AI 助手，以团队虚拟成员的身份工作。
当前频道实例：{instance.id}（平台 {instance.platform}）。

行为规范：
- 使用频道历史与团队记忆回答，避免让用户重复解释背景。
- 回答使用中文，简洁、可直接行动。
- 未获授权的工具不得调用；调用工具前先说明意图。

可用工具：{allowed}
"""
    if role:
        prompt += f"\n角色设定：{role}\n"
    if tag_instruction:
        prompt += f"\n当前任务预设指令：\n{tag_instruction}\n"
    if output_style:
        prompt += f"\n输出风格：{output_style}\n"
    if extra_context:
        prompt += f"\n频道上下文补充：\n{extra_context}\n"
    return prompt
