# TeamAI

嵌入企业通讯平台的共享 AI 协作成员。在 Slack 或飞书的频道里 @ 它派活，它读频道上下文、拆解并执行任务、在原线程回帖。一个频道一个共享实例，团队全员共用同一份记忆与预算。

对标 Anthropic 的 Claude Tag，需求与设计见 `docs/`。

## 能做什么

- **同步问答**：闲聊、查询类消息秒级回复，走轻量模型
- **异步长任务**：代码审查、Bug 修复、数据分析、文档、PR 操作这类要多轮工具调用的意图自动入队，交 worker 进程执行，完成后回帖。判据是「耗时可能超过平台 3 秒事件窗口」，不是「是否用贵模型」
- **频道记忆**：从对话中积累知识并在后续任务中检索复用，频道间默认隔离
- **预算与审计**：按频道核算 token 配额，超限暂停；每个动作留审计记录
- **权限策略**：按频道白名单控制可用工具

未实现：Ambient Mode 主动介入、对话标签模板复用、端到端评测集（`docs/tasklist.md` 第 11 / 12 / 13 项）。

## 快速开始

需要 Python ≥ 3.11、Docker、[uv](https://docs.astral.sh/uv/)。

```bash
make install          # 建 .venv 并装依赖
make config           # 从示例生成 config/config.yaml 与 .env（已存在则跳过）
```

编辑 `.env` 填凭据，至少要有 LLM 的 key 和一个平台的凭据。然后：

```bash
make up               # 起依赖容器：postgres + redis + qdrant
make migrate          # 建表
make run-web          # 起 web 进程（Admin API + 平台入口）
make run-worker       # 另开一个终端，起 worker（消费队列 + 定时任务）
```

验证：

```bash
curl localhost:8000/api/health
make verify-longtask  # 冒烟长任务链路，需 redis 已起
```

`make` 或 `make help` 看全部目标。

## 配置

分两个文件，按「是否敏感」划线。两者都不入库，优先级是 环境变量 > `.env` > `config/config.yaml` > 代码里的字段默认值。

| 文件 | 放什么 | 示例 |
|---|---|---|
| `.env` | 凭据与连接串 | token、API key、`DATABASE_URL` |
| `config/config.yaml` | 非敏感可调项 | 模型分级、端口、超时阈值、队列名 |

yaml 里的嵌套分组只为可读，加载时展平成平铺字段（`model.full` → `settings.model_full`），所以任何一项也能用环境变量临时覆盖，变量名即展平后的大写形式。

两个文件缺失或留空都不报错，直接回落默认值。各字段的含义与取值见 `config/config.example.yaml` 和 `.env.example`，注释比这里详细。

### 换模型供应商

模型 ID 取 `provider:model` 形式，**provider 段同时决定协议与端点**，所以换供应商只改配置：

```yaml
# config/config.yaml
model:
  light_primary: openai-chat:gpt-5-mini    # /v1/chat/completions
  light_fallback: deepseek:deepseek-chat   # 自带端点
  full: anthropic:claude-opus-4-8          # /v1/messages
```

```bash
# .env
LLM_API_KEY=sk-xxx
LLM_BASE_URL=https://your-gateway.example.com/v1   # 留空走各家官方地址
```

`openai:` 打 Responses API，`openai-chat:` 打 Chat Completions —— 第三方中转多为后者。另支持 ollama / openrouter / litellm / moonshotai / azure / bedrock 等，全表见 pydantic-ai 的 providers 目录。不带前缀的裸名按 `anthropic:` 处理。

`config/config.example.yaml` 的 model 段有六个成套的例子（含各自需要的 `.env`），整块取消注释即可用。那些例子由 `tests/unit/test_config_examples.py` 直接读取并实际装配验证，不会与代码脱节。

三档可以混用不同供应商，但 `LLM_BASE_URL` 是全局一份，混用时最好让各家都走官方端点。

## 平台接入

两个平台都支持「HTTP 回调」和「长连接」两种接入方式。长连接不需要公网可达的回调 URL，适合本地调试。

| 平台 | 凭据齐备的判据 | 接入方式 |
|---|---|---|
| Slack | `SLACK_BOT_TOKEN` + `SLACK_SIGNING_SECRET` | 配了 `SLACK_APP_TOKEN` 走 Socket Mode，否则 Events API（`POST /slack/events`） |
| 飞书 | `FEISHU_APP_ID` + `FEISHU_APP_SECRET` | 配了 `FEISHU_ENCRYPT_KEY` + `FEISHU_VERIFICATION_TOKEN` 走回调（`POST /feishu/events`），否则 ws 长连接 |

凭据不全的平台会被静默跳过（不报错），只跑 Admin API。推断结果可用 `platforms.<平台>.mode` 显式覆盖。

飞书侧还需在[开放平台](https://open.feishu.cn)开启机器人能力、开通消息读写权限、订阅 `im.message.receive_v1` 事件。启动时会调 `/open-apis/bot/v3/info` 拉 bot 的 `open_id`，群聊里的 @ 判定完全依赖它 —— 拉取失败只告警不中断启动，但群里 @ 机器人不会有反应（单聊不受影响）。

## 架构

依赖只向下，由 `tests/unit/test_layering.py` 用 AST 静态校验锁死。

```
adapters/        平台适配与 Admin API（唯一的 SDK 依赖点）
  ├── slack/     app.py + translator.py
  ├── feishu/    crypto / translator / callback / ws / connector
  └── admin/     FastAPI 路由，按资源分模块
application/     用例编排：router / intent / orchestrator / budget / memory / tag
  └── agent/     runtime / context / prompts
domain/          模型、端口、仓储接口、领域服务（零内部依赖）
infrastructure/  DB / 仓储实现 / 队列 / 向量库 / 调度 / LLM 网关 / 工具 / 出向消息
```

`application` 与 `domain` 不出现任何平台词汇或 SDK 依赖，接新平台只需加一个 `adapters/<平台>/` 子包并在 `app/backend/main.py` 的 `CONNECTOR_BUILDERS` 登记。

### 两个进程

| 进程 | 职责 | 入口 |
|---|---|---|
| web | 收平台事件、同步回复、Admin API | `python -m app.backend.main` |
| worker | 消费长任务队列、跑定时任务 | `python -m app.worker.main` |

切开的理由：长任务是小时/天级，与毫秒级的平台事件放同一进程会互相拖累 —— 一个长任务卡住事件循环就会让事件超时重投；而 web 按 QPS 扩容也会把 worker 副本一并放大，导致同一任务被多次执行。

worker 里两个定时任务都不可缺：预算周期重置（否则耗尽配额的频道永久停在 `EXHAUSTED`）、超时巡检（否则 worker 崩溃时在执行的任务永久停在 `RUNNING`，发起人等不到回复）。

## Admin API

前缀 `/api`，监听地址见 `admin_api.*`。

| 方法与路径 | 用途 |
|---|---|
| `GET /api/health` | 健康检查 |
| `GET/POST /api/channels/{id}/memories`、`DELETE /api/memories/{entry_id}` | 频道记忆 |
| `GET/PUT /api/channels/{id}/budget` | token 预算 |
| `GET/PUT /api/channels/{id}/policy` | 权限策略 |
| `GET /api/channels/{id}/tasks` | 任务查询 |
| `GET /api/channels/{id}/audit` | 审计查询 |
| `GET/POST /api/channels/{id}/tags` | 标签 |

## 开发

```bash
make lint       # ruff 检查
make fmt        # ruff 自动修复
make test       # 全量测试
make test-cov   # 带覆盖率
make check      # lint + test，提交前跑这个
```

测试分 `tests/unit/` 与 `tests/integration/`，不依赖外部服务（用 fake 替身），`make test` 可离线跑。

数据库有两条建表路径：生产走 `alembic upgrade head`（`make migrate`），测试与本机开发走 `init_db` 的 `create_all`。两边都以 `Base.metadata` 为源（见 `migrations/env.py`），但目前没有自动化的漂移检查 —— 改 ORM 后要记得补迁移。

进程必须从仓库根启动 —— `config/config.yaml` 与 `.env` 都相对当前工作目录解析。`make run-*` 与容器里的 `WORKDIR` 都保证了这一点。

## 部署

web 与 worker 共用一份镜像，靠启动命令区分：

```bash
make build                                      # 构建镜像
docker run --rm teamai:latest                   # 默认 web，启动前自动跑迁移
docker run --rm teamai:latest python -m app.worker.main   # worker
```

配置不进镜像，运行时挂载或用环境变量注入：

```bash
docker run -v ./config:/app/config:ro --env-file .env -p 8000:8000 teamai:latest
```

镜像以非 root 用户运行，健康检查打 `/api/health`（worker 容器没有 HTTP 端口，需覆盖或 `--no-healthcheck`）。

`deploy/docker-compose.yml` 只管依赖服务（postgres + redis + qdrant），不含应用本身。开发机上端口撞了就在 `.env` 里改 `POSTGRES_PORT` / `REDIS_PORT` / `QDRANT_PORT`，记得连接串跟着改。

## 文档

| 文件 | 内容 |
|---|---|
| `docs/PRD-claude-tag.md` | 产品需求 |
| `docs/Design-claude-tag.md` | 总体设计 |
| `docs/Design-multi-platform.md` | 多平台接入设计 |
| `docs/Code-Design-Python.md` | 代码结构与分层约定 |
| `docs/tasklist.md` | 实施进度 |
