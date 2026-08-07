"""应用配置（pydantic-settings 类型安全加载）。"""

from __future__ import annotations

from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Slack
    slack_bot_token: str = ""
    slack_signing_secret: str = ""
    slack_app_token: str = ""

    # LLM 模型分级
    anthropic_api_key: str = ""
    model_light_primary: str = "claude-sonnet-4-5"
    model_light_fallback: str = "claude-3-5-haiku"
    model_full: str = "claude-opus-4-8"

    # Database
    database_url: str = "postgresql+asyncpg://teamai:teamai@localhost:5432/teamai"

    # Redis / Queue
    redis_url: str = "redis://localhost:6379/0"
    arq_queue_name: str = "teamai-tasks"
    # 事件去重记录的存活时间。默认 1 小时，覆盖 Slack 的重投窗口
    # （官方三次重投在约半小时内发完），到期即淘汰以免白占内存。
    event_dedup_ttl_seconds: int = 3600

    # Vector
    qdrant_url: str = "http://localhost:6333"
    qdrant_collection: str = "teamai-memory"

    # Budget
    budget_period: Literal["MONTHLY", "DAILY", "WEEKLY"] = "MONTHLY"

    # Admin API
    admin_api_host: str = "0.0.0.0"
    admin_api_port: int = 8000

    # 上下文压缩
    context_max_messages: int = 60
    context_summary_threshold: int = 120

    @property
    def slack_enabled(self) -> bool:
        """Slack 凭据是否齐备。缺任一项则只跑 Admin API，不接入 Slack。

        `slack_app_token` 不在此列：它只决定接入方式（配了走 Socket Mode，
        没配走 Events API），不决定是否接入。
        """
        return bool(self.slack_bot_token and self.slack_signing_secret)


settings = Settings()
