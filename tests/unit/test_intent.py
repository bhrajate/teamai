"""IntentClassifier 与 Intent 判据测试。

is_long_running 决定消息走同步还是异步链路，判错的代价是两头的：判成短任务
会让长耗时请求撑爆平台的 3s 响应窗口；判成长任务会让一句「现在几点」白绕
一趟队列、还得等 worker 轮询。故这里把两个集合的成员都钉住。
"""

from __future__ import annotations

import pytest

from teamai.application.intent import Intent, IntentClassifier


@pytest.mark.parametrize(
    "text,expected",
    [
        ("帮我 review 这个 PR", "code_review"),
        ("这里有个 bug 帮我修", "bugfix"),
        ("看下这周的销售数据", "data_analysis"),
        ("帮我写个文档", "documentation"),
        ("提个 PR", "pr_operation"),
        ("建个工单跟进", "ticket"),
    ],
)
async def test_关键词命中对应意图(text: str, expected: str) -> None:
    intent = await IntentClassifier().classify(text)
    assert intent.kind == expected


async def test_疑问句式判为查询且置信度偏低() -> None:
    intent = await IntentClassifier().classify("什么是幂等")
    assert intent.kind == "query"
    assert intent.confidence == 0.4
    assert intent.is_task is False


async def test_无关键词无句式回落general_task() -> None:
    intent = await IntentClassifier().classify("把这段话润色一下")
    assert intent.kind == "general_task"
    assert intent.is_task is True


# ===== 两个派生判据 =====


@pytest.mark.parametrize(
    "kind", ["code_review", "bugfix", "data_analysis", "documentation", "pr_operation"]
)
def test_长任务意图走异步(kind: str) -> None:
    assert Intent(kind=kind).is_long_running is True


@pytest.mark.parametrize("kind", ["query", "chat", "ticket", "general_task"])
def test_短任务意图走同步(kind: str) -> None:
    assert Intent(kind=kind).is_long_running is False


@pytest.mark.parametrize("kind", ["code_review", "bugfix", "data_analysis"])
def test_高阶模型意图(kind: str) -> None:
    assert Intent(kind=kind).model_level == "full"


@pytest.mark.parametrize("kind", ["query", "chat", "documentation", "pr_operation", "general_task"])
def test_轻量模型意图(kind: str) -> None:
    assert Intent(kind=kind).model_level == "light"


def test_两个判据互相独立() -> None:
    """documentation 用 light 档却要异步 —— 记录这个刻意的不重合。

    若哪天有人把 is_long_running 实现成 model_level == "full" 的别名，
    这条会红。
    """
    doc = Intent(kind="documentation")
    assert doc.model_level == "light"
    assert doc.is_long_running is True
