"""意图分类：识别任务意图（LLM 零样本分类 + 关键词规则兜底）。"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass
class Intent:
    kind: str
    confidence: float = 1.0

    @property
    def is_task(self) -> bool:
        return self.confidence >= 0.5

    @property
    def model_level(self) -> str:
        """该意图所需的模型档位。

        与 kind 的定义放在同一文件：新增 kind 时能一眼看到要不要进
        _FULL_MODEL_KINDS，避免这份映射散到调用方（原先硬编在 router 里）。
        """
        return "full" if self.kind in _FULL_MODEL_KINDS else "light"

    @property
    def is_long_running(self) -> bool:
        """是否走异步链路（入队交 worker 执行）。

        与 model_level 同理放在 kind 旁边。判据是「耗时是否可能超过平台的
        事件响应窗口」，不是「是否用贵模型」—— 两个集合恰好高度重合但语义
        不同，故分开定义：documentation 用 light 档却要多轮工具调用，
        属于长任务；未来若出现「贵但一轮出结果」的意图也不必被迫异步。
        """
        return self.kind in _LONG_RUNNING_KINDS


_QUERY_KINDS = {"query", "chat"}
_TASK_KEYWORDS = [
    ("code_review", {"审查", "review", "code review", "看下代码"}),
    ("bugfix", {"bug", "修复", "修 bug", "异常", "报错", "error"}),
    ("data_analysis", {"数据", "指标", "统计", "报表", "分析", "dashboard", "sales"}),
    ("documentation", {"文档", "写文档", "总结", "汇总", "记录"}),
    ("pr_operation", {"pr", "pull request", "提 pr", "合并"}),
    ("ticket", {"工单", "ticket", "case"}),
]

# 需要高阶模型的意图：推理链长、改动有风险，light 档位不够用
_FULL_MODEL_KINDS = {"code_review", "bugfix", "data_analysis"}

# 需要异步执行的意图：要读代码/查数据/操作外部系统，多轮工具调用，
# 耗时不可控且可能远超平台的事件响应窗口（Slack/飞书均为 3s）。
# 其余意图（query / chat / ticket / general_task）是单轮生成，秒级返回，
# 同步回复的体验明显更好，不值得多绕一趟队列。
_LONG_RUNNING_KINDS = {"code_review", "bugfix", "data_analysis", "documentation", "pr_operation"}


class IntentClassifier:
    def __init__(self, llm: object | None = None) -> None:
        self._llm = llm  # 预留：可注入 LLM 零样本分类器

    async def classify(self, text: str) -> Intent:
        """先关键词规则兜底，命中即返回；未命中走 LLM 分类。

        当前无 LLM 分类器时默认视为可执行任务，由 Agent 自主判断。
        """
        lowered = text.lower()
        for kind, keywords in _TASK_KEYWORDS:
            for kw in keywords:
                if kw in lowered or kw in text:
                    return Intent(kind=kind)
        if self._llm is not None:
            return await self._llm.classify(text)
        # 无分类器且无关键词：若为疑似提问句式则视为查询，否则交给 Agent
        if re.search(r"[?？]|请问|什么是|如何|为什么|多少", text):
            return Intent(kind="query", confidence=0.4)
        return Intent(kind="general_task")
