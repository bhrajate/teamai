"""校验 config.example.yaml 里 model 段的每个示例都真的能装配。

文档里的配置例子若写错前缀、或与它标注的 .env 不配套，用户照抄就踩坑，而
test_llm_gateway.py 发现不了 —— 那里用的是测试自己写死的字符串，不读 example。
这组测试直接把 example 文件里的注释块取消注释来跑，故文档一旦漂移就会红。

不发真实请求，只验「配置 → 模型类 + 端点」。

守得住：provider 名写错或拼错（装配即抛）、模型名传丢、base_url 该采用却没采用
或不该采用却采用了、整个示例块被删导致某种接法没例子可抄。

守不住：块内单行前缀改错而同块其他行仍是原前缀 —— 协议集合不变，覆盖断言看不见。
要抓这种得解析标题散文与配置是否一致，而标题是自由文本、例 6 又刻意混用多家，
那层启发式比它防的错更容易坏，故不做。改 example 时仍需人眼核对标题与配置相符。
"""

from __future__ import annotations

import inspect
import os
import pathlib
import re
import tempfile
from collections.abc import Iterator

import pytest
import yaml
from pydantic_ai.models import parse_model_id
from pydantic_ai.providers import infer_provider_class

EXAMPLE = pathlib.Path(__file__).resolve().parents[2] / "config" / "config.example.yaml"

# 示例块形如：
#   # —— 例 2：OpenAI 兼容网关 ——
#   # .env: LLM_API_KEY=sk-xxx
#   #       LLM_BASE_URL=https://gw.example.com/v1
#   #model:
#   #  full: openai-chat:gpt-5
# 尾部 —— 前的空格可有可无：标题以「）」收尾时紧贴 ——
TITLE = re.compile(r"^#\s*——\s*(.+?)\s*——")
ENV_LINE = re.compile(r"^#\s*(?:\.env:)?\s*(LLM_[A-Z_]+)=(\S*)")


def _parse() -> list[tuple[str, dict[str, str], str]]:
    """切出 [(标题, 该例标注的 .env, model 段 yaml)]，含未注释的默认块。"""
    out: list[tuple[str, dict[str, str], str]] = []
    title, env, body = "默认", {}, []

    def flush() -> None:
        if body and body[0].startswith("model:"):
            out.append((title, dict(env), "\n".join(body)))

    for line in EXAMPLE.read_text(encoding="utf-8").splitlines():
        if m := TITLE.match(line):
            flush()
            title, env, body = m.group(1), {}, []
            continue
        if m := ENV_LINE.match(line):
            env[m.group(1)] = m.group(2)
            continue
        stripped = line[1:] if line.startswith("#") else line
        if stripped.startswith("model:") or (body and stripped.startswith("  ")):
            body.append(stripped)
        elif body:
            flush()
            body = []
    flush()
    return out


EXAMPLES = _parse()


@pytest.fixture
def _in_tmp_config() -> Iterator[pathlib.Path]:
    """Settings 按 CWD 解析 config/config.yaml 与 .env，故须切到临时目录。"""
    old = pathlib.Path.cwd()
    with tempfile.TemporaryDirectory() as tmp:
        cwd = pathlib.Path(tmp)
        (cwd / "config").mkdir()
        try:
            os.chdir(cwd)
            yield cwd
        finally:
            os.chdir(old)


def _takes_base_url(model_id: str) -> bool:
    """该 provider 是否接受 base_url（与 gateway._provider 同一判据）。"""
    name, _ = parse_model_id(model_id)
    params = inspect.signature(infer_provider_class(name or "anthropic").__init__).parameters
    return "base_url" in params


def test_解析到了全部示例() -> None:
    """解析逻辑失效会让下面的用例静默变成零条，故先钉住数量下界与默认块。"""
    assert len(EXAMPLES) >= 6
    assert EXAMPLES[0][0].startswith("默认")


def test_示例覆盖全部协议形态() -> None:
    """example 的价值在于「每种接法都给了一份能抄的配置」，故覆盖面本身要守。

    单看某一条示例装配成功不足以发现前缀写错 —— 把 `openai-chat:` 误写成
    `anthropic:` 时模型名与端点都还对得上，只有协议悄悄变了。改成检查所有
    示例合起来必须覆盖下列形态，任一条被改坏或删掉，对应形态即缺失。
    """
    prefixes = {
        parse_model_id(mid)[0] or "anthropic"
        for _, _, body in EXAMPLES
        for mid in yaml.safe_load(body)["model"].values()
    }
    missing = {
        "anthropic": "Anthropic 原生 /v1/messages",
        "openai-chat": "OpenAI /v1/chat/completions",
        "openai": "OpenAI /v1/responses",
        "deepseek": "自带固定端点、忽略 base_url",
        "ollama": "本地部署",
    }.keys() - prefixes
    assert not missing, f"example 的 model 段缺少这些接法的示例: {sorted(missing)}"


def test_每种前缀都有带base_url与不带的示例() -> None:
    """自定义端点是这次改动的主要诉求，两种情形都得有例子可抄。"""
    with_base = {bool(env.get("LLM_BASE_URL")) for _, env, _ in EXAMPLES}
    assert with_base == {True, False}, "需同时给出「填 LLM_BASE_URL」与「留空」的示例"


@pytest.mark.parametrize(
    ("title", "env", "model_yaml"),
    EXAMPLES,
    ids=[t.split("：")[0] for t, _, _ in EXAMPLES],
)
def test_示例可装配且端点正确(
    title: str, env: dict[str, str], model_yaml: str, _in_tmp_config: pathlib.Path
) -> None:
    from teamai.config import Settings
    from teamai.infrastructure.llm.gateway import ModelConfig, PydanticAIGateway

    (_in_tmp_config / "config" / "config.yaml").write_text(model_yaml + "\n", encoding="utf-8")
    (_in_tmp_config / ".env").write_text(
        "".join(f"{k}={v}\n" for k, v in env.items()), encoding="utf-8"
    )

    gw = PydanticAIGateway(ModelConfig.from_settings(Settings()))
    primary, fallback = gw._model("light").models
    declared = yaml.safe_load(model_yaml)["model"]
    base = env.get("LLM_BASE_URL", "")

    for label, model in (
        ("full", gw._model("full")),
        ("light_primary", primary),
        ("light_fallback", fallback),
    ):
        ident = declared[label]
        # 前缀被剥掉，模型名原样传给 SDK
        assert model.model_name == ident.split(":", 1)[-1], f"{title}/{label} 模型名不符"
        # 端点：接受 base_url 的须采用；自带固定端点的须忽略（文档承诺）
        adopted = model.base_url.rstrip("/").startswith(base.rstrip("/")) if base else False
        if base and _takes_base_url(ident):
            assert adopted, f"{title}/{label} 未采用 .env 的 LLM_BASE_URL"
        elif base:
            assert not adopted, f"{title}/{label} 本不该接受 base_url"
        assert model.base_url, f"{title}/{label} 无端点"
