"""ContextBundle：Agent 单次 run 的上下文组装与压缩。"""

from __future__ import annotations

from dataclasses import dataclass, field

from teamai.domain.channel import ChannelInstance
from teamai.domain.memory import MemoryEntry
from teamai.domain.policy import PermissionPolicy
from teamai.domain.tag import TagTemplate


@dataclass
class ContextBundle:
    task_id: str
    channel_instance_id: str
    user_prompt: str
    system_prompt: str
    model_level: str
    instance: ChannelInstance
    policy: PermissionPolicy | None
    allowed_tools: list[str] = field(default_factory=list)
    memory_hits: list[MemoryEntry] = field(default_factory=list)
    thread_history: list[str] = field(default_factory=list)
    tag: TagTemplate | None = None

    @property
    def memory_context(self) -> str:
        """将记忆命中拼为上下文文本。"""
        if not self.memory_hits:
            return ""
        lines = []
        for entry in self.memory_hits:
            lines.append(f"- {entry.content}")
        return "\n".join(lines)

    def compact(self, max_history: int, summary_threshold: int) -> "ContextBundle":
        """上下文压缩：线程历史超阈值时，将最旧部分摘要为一行提示。

        返回新 bundle，不修改自身（保持可追溯）。
        """
        if len(self.thread_history) <= max_history:
            return self
        dropped = len(self.thread_history) - max_history
        recent = self.thread_history[-max_history:]
        summary = f"[已压缩 {dropped} 条更早消息，历史早于此处；如需细节请明确询问]"
        merged = [summary] + recent
        new_bundle = ContextBundle(
            task_id=self.task_id,
            channel_instance_id=self.channel_instance_id,
            user_prompt=self.user_prompt,
            system_prompt=self.system_prompt,
            model_level=self.model_level,
            instance=self.instance,
            policy=self.policy,
            allowed_tools=list(self.allowed_tools),
            memory_hits=list(self.memory_hits),
            thread_history=merged,
            tag=self.tag,
        )
        return new_bundle
