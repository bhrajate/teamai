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


# 蒸馏结果里表示「本窗口没有值得记的内容」的标记。
# 必须给模型一个明确的空结果出口：绝大多数对话窗口本就不该产出任何记忆，
# 若只要求「提取事实」，模型会为了满足输出格式而把寒暄硬编成事实。
DISTILL_NONE = "NONE"

DISTILL_SYSTEM_PROMPT = f"""你在从团队聊天记录中提取值得长期保存的知识。

只提取满足以下全部条件的内容：
- 跨会话仍然有用（项目背景、技术决策、约定的流程、系统的固定参数）
- 陈述的是事实或决定，而非猜测与讨论过程
- 不依赖当时的临时语境就能读懂

必须排除：
- 寒暄、情绪表达、玩笑、附和（「收到」「好的」「哈哈」）
- 一次性的问答与临时状态（「我马上看」「刚重启了」）
- 无法脱离上下文理解的片段
- 个人隐私、凭据、密钥

输出格式：每行一条，以 `类型|内容` 形式给出，类型取以下之一：
- BACKGROUND_KNOWLEDGE：项目或系统的背景知识
- DECISION：团队做出的决定
- FACT：具体的事实与参数
- PREFERENCE：团队对协作方式或输出形式的偏好

内容用陈述句写完整，不要用「他说」这类转述。若整段对话没有任何符合条件的内容，
只输出 {DISTILL_NONE}，不要解释、不要输出其他任何文字。"""


def build_distill_prompt(lines: list[str]) -> str:
    """把一个对话窗口拼成蒸馏用的用户提示词。"""
    body = "\n".join(lines)
    return f"以下是一个团队频道的连续对话片段：\n\n{body}\n\n请按系统提示词的规则提取。"
