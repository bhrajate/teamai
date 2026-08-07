"""应用配置（pydantic-settings 类型安全加载）。

配置分两处，按「是否敏感」划线：

- `config/config.yaml`（不入库）：非敏感可调项 —— 模型分级、上下文压缩阈值、
  端口、队列名等。嵌套分组便于阅读，加载时展平成平铺字段。
- `.env`（不入库）：凭据与连接串 —— token、API key、数据库/Redis/Qdrant 地址。
  这些每套环境都不同，且不该出现在任何可能被提交的文件里。

优先级 环境变量 > .env > config/config.yaml > 字段默认值。默认值仍以本文件为准：
config.yaml 不入库，故它只承载本机覆盖，`config/config.example.yaml` 起文档作用。
两个文件缺失都不报错，直接回落默认值。

两个路径都相对**当前工作目录**解析（`.env` 沿用 pydantic-settings 的既有行为），
故须从仓库根目录启动进程 —— `make run-web` / `make run-worker` 与容器里的
WORKDIR 都保证了这一点。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    YamlConfigSettingsSource,
)


def _flatten(data: dict[str, Any], parent: str = "") -> dict[str, Any]:
    """把嵌套 dict 按下划线拼成平铺 key：{"model": {"full": x}} -> {"model_full": x}。"""
    out: dict[str, Any] = {}
    for key, value in data.items():
        name = f"{parent}_{key}" if parent else key
        if isinstance(value, dict):
            out.update(_flatten(value, name))
        else:
            out[name] = value
    return out


class FlatYamlSettingsSource(YamlConfigSettingsSource):
    """读 yaml 并展平嵌套键。

    pydantic-settings 自带的 YamlConfigSettingsSource 按字段名匹配顶层键，
    嵌套 dict 会原样传给校验器并因 extra_forbidden 报错。这里先展平，
    从而做到「yaml 里分组可读、代码里仍是平铺的 settings.xxx」，
    二十来处调用点无需改动。
    """

    def __call__(self) -> dict[str, Any]:
        return _flatten(super().__call__())


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        yaml_file="config/config.yaml",
        yaml_file_encoding="utf-8",
        extra="ignore",
    )

    # ===== 以下走 .env：凭据与连接串 =====

    # Slack
    slack_bot_token: str = ""
    slack_signing_secret: str = ""
    slack_app_token: str = ""

    # LLM 凭据
    anthropic_api_key: str = ""

    # 连接串
    database_url: str = "postgresql+asyncpg://teamai:teamai@localhost:5432/teamai"
    redis_url: str = "redis://localhost:6379/0"
    qdrant_url: str = "http://localhost:6333"

    # ===== 以下走 config/config.yaml：非敏感可调项 =====

    # model.*
    model_light_primary: str = "claude-sonnet-4-5"
    model_light_fallback: str = "claude-3-5-haiku"
    model_full: str = "claude-opus-4-8"

    # queue.*
    queue_name: str = "teamai-tasks"

    # event_dedup.*
    event_dedup_ttl_seconds: int = 3600

    # qdrant.*（url 是连接串，走 .env）
    qdrant_collection: str = "teamai-memory"

    # budget.*
    budget_period: Literal["MONTHLY", "DAILY", "WEEKLY"] = "MONTHLY"

    # admin_api.*
    admin_api_host: str = "0.0.0.0"
    admin_api_port: int = 8000

    # context.*
    context_max_messages: int = 60
    context_summary_threshold: int = 120

    @property
    def slack_enabled(self) -> bool:
        """Slack 凭据是否齐备。缺任一项则只跑 Admin API，不接入 Slack。

        `slack_app_token` 不在此列：它只决定接入方式（配了走 Socket Mode，
        没配走 Events API），不决定是否接入。
        """
        return bool(self.slack_bot_token and self.slack_signing_secret)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """加载顺序即优先级，靠前者胜。

        yaml 排在 dotenv 之后：凭据与部署差异由环境变量/.env 决定，
        yaml 只提供可调项的取值。
        """
        return (
            init_settings,
            env_settings,
            dotenv_settings,
            FlatYamlSettingsSource(settings_cls),
            file_secret_settings,
        )


settings = Settings()
