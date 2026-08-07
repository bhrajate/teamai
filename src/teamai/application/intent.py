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


_QUERY_KINDS = {"query", "chat"}
_TASK_KEYWORDS = [
    ("code_review", {"审查", "review", "code review", "看下代码"}),
    ("bugfix", {"bug", "修复", "修 bug", "异常", "报错", "error"}),
    ("data_analysis", {"数据", "指标", "统计", "报表", "分析", "dashboard", "sales"}),
    ("documentation", {"文档", "写文档", "总结", "汇总", "记录"}),
    ("pr_operation", {"pr", "pull request", "提 pr", "合并"}),
    ("ticket", {"工单", "ticket", "case"}),
]


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
