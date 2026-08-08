"""大模型调用端口。

与 queue / dedup 同理：契约由领域层声明，infrastructure 层用具体 SDK 实现。

端口只覆盖「发起一次带工具的模型调用」。预算核算、上下文压缩、审计留痕、
超限转 PAUSED 都是用例层的策略，不在此列。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from teamai.domain.ports.tools import ToolBundle


@dataclass
class LLMResult:
    output: str
    tokens: int = 0


class TokenBudgetExceeded(Exception):
    """本次调用触达 token 上限。

    实现方须把底层 SDK 的超限信号翻译成本异常。用例层据此把任务转 PAUSED，
    从而不必 catch 某个具体 SDK 的异常类型 —— 否则换 SDK 会直接改坏状态机。
    """


class LLMGateway(ABC):
    """一次模型调用。实现方负责模型选择、重试与工具执行循环。"""

    @abstractmethod
    async def run(
        self,
        prompt: str,
        *,
        model_level: str,
        system_prompt: str = "",
        tools: ToolBundle | None = None,
        token_limit: int | None = None,
    ) -> LLMResult:
        """执行一次调用并返回输出与 token 消耗。

        Args:
            prompt: 用户提示词（已含记忆与线程历史等上下文）。
            model_level: 模型档位，``light`` 或 ``full``。具体模型 ID 由实现方决定。
            system_prompt: 系统提示词。
            tools: 已按频道权限裁剪的工具集，见 :data:`ToolBundle`。
            token_limit: 本次调用的 token 上限；触达时抛 :class:`TokenBudgetExceeded`。
        """
        ...
