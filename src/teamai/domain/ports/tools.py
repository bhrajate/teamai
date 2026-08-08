"""工具提供方端口。

领域层只关心「按频道白名单取一份工具集」这件事，不关心工具集长什么样。
故 ``ToolBundle`` 是**不透明句柄**：领域与用例层都不得对它做任何解构，
只负责原样递给 :class:`~teamai.domain.ports.llm.LLMGateway`；
只有 infrastructure 层的实现方知道它实际是 pydantic-ai 的 toolset。

这里刻意不定义「工具描述符」（name/description/参数 schema）那类结构。
若在领域层重新描述工具，就得由 infrastructure 把它翻译回 SDK 的工具对象，
翻译层一旦失真就会丢掉参数 schema 与调用前校验 —— 那正是本项目此前
自造 ``BaseTool`` 踩过的坑。工具的形状交给 SDK，领域只管授权范围。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TypeAlias

ToolBundle: TypeAlias = object
"""一份已按频道权限裁剪好的工具集。对领域层不透明，不可解构。"""


class ToolProvider(ABC):
    """按频道白名单裁剪工具集。实现方负责与具体 Agent SDK 交互。"""

    @abstractmethod
    def for_channel(self, allowed: list[str]) -> ToolBundle | None:
        """裁出本次调用可见的工具集。

        未授权的工具不应出现在返回的工具集中 —— 模型无法调用它看不见的工具，
        因此鉴权只发生在这一步，不需要在工具执行时二次检查。

        无任何可用工具时返回 ``None``，表示本次调用不挂工具。
        """
        ...
