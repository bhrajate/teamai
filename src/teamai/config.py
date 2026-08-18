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

from pydantic import AliasChoices, Field
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

    # 飞书（app_id + app_secret 两种模式都要；encrypt_key / verification_token
    # 仅 callback 模式需要，只影响 mode 推断）
    feishu_app_id: str = ""
    feishu_app_secret: str = ""
    feishu_encrypt_key: str = ""
    feishu_verification_token: str = ""

    # LLM 凭据。留空则不传给 provider，由各家 SDK 自行读它认的环境变量
    # （AnthropicProvider 读 ANTHROPIC_API_KEY，OpenAIProvider 读 OPENAI_API_KEY）。
    # 兼容旧名 ANTHROPIC_API_KEY：协议可配之后 key 不再专属 Anthropic，
    # 故字段改叫 llm_api_key，但旧 .env 无须改动（新名优先）。
    llm_api_key: str = Field("", validation_alias=AliasChoices("llm_api_key", "anthropic_api_key"))
    # 自定义端点。留空走各 provider 的官方地址；填了则注入到 provider
    # （仅对接受 base_url 的 provider 生效，如 anthropic / openai / ollama；
    # deepseek 这类自带固定端点的会忽略它）。
    llm_base_url: str = ""

    # 连接串
    database_url: str = "postgresql+asyncpg://teamai:teamai@localhost:5432/teamai"
    redis_url: str = "redis://localhost:6379/0"
    qdrant_url: str = "http://localhost:6333"

    # ===== 以下走 config/config.yaml：非敏感可调项 =====

    # model.*
    # 取 `provider:model` 形式，provider 段同时决定**协议**与端点，例如
    # anthropic:（/v1/messages）、openai-chat:（/v1/chat/completions）、
    # openai:（/v1/responses）、deepseek: / ollama: 等。
    # 不带前缀的裸名按 anthropic 处理（见 llm/gateway.py 的 _normalize），
    # 故下面三个默认值与旧配置都无须改动。
    model_light_primary: str = "claude-sonnet-4-5"
    model_light_fallback: str = "claude-3-5-haiku"
    model_full: str = "claude-opus-4-8"

    # queue.*
    queue_name: str = "teamai-tasks"

    # event_dedup.*
    event_dedup_ttl_seconds: int = 3600

    # qdrant.*（url 是连接串，走 .env）
    qdrant_collection: str = "teamai-memory"

    # embedding.*
    # 留空即关闭语义检索：记忆检索回落到「按时间倒序取最近若干条」，功能退化
    # 但不报错。改造前的实现是「字段留着但组合根从不注入」，于是向量链路从未
    # 运行过而无人察觉 —— 现在这个开关是显式的。
    embedding_model: str = ""
    # 维度必须与所选模型一致：向量库建集合时要用它，且 Qdrant 的集合维度
    # 创建后不可修改。text-embedding-3-small / bge-large-zh 是 1536，
    # bge-small-zh 是 512，multilingual-e5-small 是 384。
    embedding_dimensions: int = 1536
    # 端点与凭据留空则复用对话用的 llm_base_url / llm_api_key ——
    # 多数部署里两者走同一个网关、同一把 key。
    embedding_base_url: str = ""
    embedding_api_key: str = ""

    # conversation.*（线程历史按需向平台拉取，不自建镜像表）
    # 单次拉取的条数上限。再多也会被 context_max_messages 压缩掉。
    conversation_history_limit: int = 30
    # 线程历史的缓存时长。缓存能自更新（每条经手的消息都 append 进去，见
    # ThreadHistorySink），故这个值不决定历史的新鲜度，只决定「多久由平台数据
    # 整体校准一次」—— 追加过程中丢的、重的、乱序的，都在下个窗口被抹平。
    # 调大省配额、调小校准更勤，两头都不会让机器人看不见刚发的消息。
    conversation_cache_ttl_seconds: int = 45

    # memory.*（记忆蒸馏：对话窗口 → 结论，原文不入库）
    # 窗口攒够这么多条就蒸馏一次
    memory_window_size: int = 20
    # 窗口静置超过这么久也蒸馏，避免冷清频道的对话一直攒着不落地
    memory_window_idle_seconds: int = 600

    # projector.*（记忆向量投影：消费 memory_outbox，见 docs/plan-memory-outbox.md）
    # 空转时的轮询间隔。取 2 秒而非分钟级：「刚写入的记忆搜不到」在同一个会话里
    # 就会被察觉 —— 蒸馏刚提炼出一条结论，紧接着的提问就该能命中它。
    projector_poll_interval_seconds: float = 2.0
    # 一轮领取多少条。逐条 embed（批量接口能省往返，但一条失败会牵连整批的重试
    # 语义，而当前写入量是每 10 分钟一轮蒸馏、一次几条，不值得为此复杂化）。
    projector_batch_size: int = 32
    # 租约时长。必须显著大于单次 embed 的最坏耗时 —— 租约提前过期会让另一个
    # 实例重复处理同一条（无害但白烧一次 embedding）。
    projector_lease_seconds: int = 300
    # 重试几次后转死信。11 次的指数退避累计约 20 分钟（封顶 300 秒/次），
    # 足够熬过多数 embedding 限流窗口；再失败就该有人看告警了。
    projector_max_attempts: int = 11

    # interactions.*（Agent 交互记录：提示词与响应全文）
    # 保留期。<= 0 表示不清理（留给「合规要求永久留存」的部署）。
    # 这张表含提示词与响应全文，不清理会无限增长 —— 既是存储负担，
    # 更是合规负担：保留期是对外承诺的一部分。
    interactions_retention_days: int = 90
    # 保留期清理的巡检间隔。默认一天一次 —— 它按 created_at 删过期行，
    # 跑得比这密只是白扫表（保留期是天级的，多删几次结果一样）。
    # 与 jobs_sweep_interval_minutes 分开正是因为这个量级差异。
    jobs_purge_interval_minutes: int = 1440

    # budget.*
    budget_period: Literal["MONTHLY", "DAILY", "WEEKLY"] = "MONTHLY"

    # jobs.*（worker 里的定时任务，见 app/worker/main.py 的 register_jobs）
    # 巡检频率。两个 job 都挂在这个间隔上：它们都只扫一遍表，跑得比这密没意义。
    jobs_sweep_interval_minutes: int = 10
    # PENDING 超时：任务已入队但迟迟没被取走。队列正常时是秒级出队，
    # 半小时没动说明 worker 全挂了或载荷坏了。
    jobs_pending_timeout_minutes: int = 30
    # RUNNING 超时：worker 取走后执行中。设计上长任务是小时/天级，故给足 24h；
    # 超过它更可能是 worker 崩在半路、任务永远停在 RUNNING。
    jobs_running_timeout_minutes: int = 24 * 60

    # admin_api.*
    admin_api_host: str = "0.0.0.0"
    admin_api_port: int = 8000
    # 控制台前端独立部署（另一个源）时必须列出其来源，否则浏览器拦掉跨源请求。
    # 逗号分隔；留空即不启用 CORS 中间件（同源部署或仅用 vite proxy 时无须配）。
    admin_api_cors_origins: str = ""
    # Admin API 访问令牌。⚠️ 属凭据，配在 .env 而非 config.yaml。
    # 留空则 /api 全部匿名可用 —— 上面挂着完整审计日志、频道记忆，以及可写的
    # 预算配额与工具白名单，公网可达时务必配上。
    admin_api_token: str = ""

    @property
    def admin_api_cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.admin_api_cors_origins.split(",") if o.strip()]

    # context.*
    context_max_messages: int = 60
    context_summary_threshold: int = 120

    # platforms.*
    # auto 保持旧行为：配了 slack_app_token 走 Socket Mode，否则 Events API
    platforms_slack_mode: Literal["auto", "events", "socket"] = "auto"
    # feishu: auto 按凭据推断（配了 encrypt_key + verification_token 走
    # callback，否则走 ws 长连接）；显式 callback / ws 覆盖推断
    platforms_feishu_mode: Literal["auto", "callback", "ws"] = "auto"
    # feishu | lark（国际版 open.larksuite.com）
    platforms_feishu_domain: Literal["feishu", "lark"] = "feishu"

    @property
    def slack_enabled(self) -> bool:
        """Slack 凭据是否齐备。缺任一项则只跑 Admin API，不接入 Slack。

        `slack_app_token` 不在此列：它只决定接入方式（配了走 Socket Mode，
        没配走 Events API），不决定是否接入。
        """
        return bool(self.slack_bot_token and self.slack_signing_secret)

    @property
    def feishu_enabled(self) -> bool:
        """飞书凭据是否齐备。只看 app_id + app_secret：两种模式都要。

        encrypt_key / verification_token 只影响 mode 推断，与 slack_enabled
        不把 slack_app_token 计入是同一思路。
        """
        return bool(self.feishu_app_id and self.feishu_app_secret)

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
