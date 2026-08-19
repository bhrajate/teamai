# TeamAI

嵌入企业通讯平台的共享 AI 协作成员。在 Slack 或飞书的频道里 @ 它派活，它读频道上下文、拆解并执行任务、在原线程回帖。一个频道一个共享实例，团队全员共用同一份记忆与预算。

对标 Anthropic 的 Claude Tag，需求与设计见 `docs/`。

## 能做什么

- **同步问答**：闲聊、查询类消息秒级回复，走轻量模型
- **异步长任务**：代码审查、Bug 修复、数据分析、文档、PR 操作这类要多轮工具调用的意图自动入队，交 worker 进程执行，完成后回帖。判据是「耗时可能超过平台 3 秒事件窗口」，不是「是否用贵模型」
- **线程上下文**：被 @ 时按需向平台拉取当前线程的最近消息，不镜像聊天记录（理由见 `docs/Design-conversation-context.md`）
- **频道记忆**：非 @ 消息先进滚动窗口，由 worker 定期蒸馏成结论后入库，原文即弃；频道间默认隔离，私聊内容不进记忆。向量索引异步建（写入与「该建索引」的意图同事务落库，worker 里的投影器秒级补上），故「刚写入到能被语义检索」之间有个可观测的延迟，见 `/metrics` 的 `teamai_memory_outbox_lag_seconds`
- **交互留痕**：每次 Agent 调用记下实际提示词、响应、生效模型与分拆的 in/out token，供复现与成本核算，按保留期清理
- **预算与审计**：按频道核算 token 配额，超限暂停；每个动作留审计记录
- **权限策略**：按频道白名单控制可用工具

- **主动介入**：按频道规则在无人 @ 时开口，例如提醒沉寂的线程。两级开关（频道总闸 + 策略里的规则）都开才生效
- **标签模板**：把角色、指令、输出风格存成可复用的配置，在频道里按名激活
- **管理控制台**：`web/` 下的前端，管上面这些配置并看任务与审计

未实现：端到端评测集（`docs/tasklist.md` 第 13 项）。标签的 `shared` 字段目前是空壳 —— 存得下、读得出，但没有任何跨频道共享语义。另有两项进行中的改造见 `tasklist.md` 21.1 / 21.2：除记忆之外的七个仓储还在各自提交事务，且 web 进程仍共用单个数据库会话（`container.py` 里注明了这是 MVP 待改）。

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
                 conversation（拉线程历史）/ distiller（蒸馏记忆）/ interaction（留痕）
  └── agent/     runtime / context / prompts
domain/          模型、端口、仓储接口、领域服务（零内部依赖）
infrastructure/  DB / 仓储实现 / 队列 / 向量库 / 调度 / LLM 网关 / embedding
                 工具 / 消息收发（出向 publisher + 入向 reader）/ 蒸馏窗口
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
| `GET /api/channels`、`GET/PATCH /api/channels/{id}` | 频道实例：列举、详情、改频道级开关 |
| `GET /api/tools` | 已注册的工具名，供策略编辑器出选项 |
| `GET /api/embedding` | embedder 装配状态（`available` / `model` / `dimensions`）。不可用时记忆能力三层降级：语义检索关闭、手工写入的冲突检查退化为字面比对、蒸馏的去重与取代对旧记忆基本失效。控制台据此在记忆页挂提示；看板看 `teamai_embedder_available`。受令牌保护而非挂在匿名的 `/health` 上 —— 「有没有配 embedding」是运营信息 |
| `GET/POST /api/channels/{id}/memories`、`PATCH/DELETE /api/memories/{entry_id}` | 频道记忆：列举、手工写入、改内容或类型、删除。改内容会**异步**重算向量索引（入队交给 worker 的投影器，故写入立刻返回、不等 embedding API）；有意不支持改可见性（`private`→`channel` 属权限变更而非内容编辑）。**写入撞上疑似重复的现行记忆时返回 409** 并带候选列表，需带 `supersede_id`（取代那条）或 `force`（并列写入）重发，两者同时给报 400；未配 embedding 时这道检查退化为字面比对，409 里 `degraded=true` |
| `GET /metrics` | Prometheus 指标。与 `/health` 一样在 Admin 令牌保护之外（抓取端是 Prometheus 而非人），但它暴露运营信息，生产应在反向代理层限来源。⚠️ 需设 `PROMETHEUS_MULTIPROC_DIR` 且 web 与 worker 指向同一目录，否则投影指标一律为 0 —— 不报错，症状是「看起来一切正常」 |
| `GET/PUT /api/channels/{id}/budget` | token 预算 |
| `GET/PUT /api/channels/{id}/policy` | 权限策略 |
| `GET /api/channels/{id}/tasks` | 任务查询 |
| `GET /api/channels/{id}/audit` | 审计查询（动作流水） |
| `GET /api/channels/{id}/interactions`、`GET /api/tasks/{task_id}/interactions`、`GET /api/interactions/{id}` | 交互记录：模型看到的提示词与响应全文。只读 —— 由运行时产生，人工写入会污染成本统计；删除走保留期巡检 |
| `GET/POST /api/channels/{id}/tags`、`PATCH/DELETE /api/channels/{id}/tags/{tag_id}` | 标签：列举、创建、启停、删除 |

未配预算或策略的频道，对应的 `GET` 返回 404 —— 那表示「还没配」，不是故障。

`PUT /budget` 是原地更新：只改上限与周期，已用量与本周期起点都保留；若新上限高于已用量，会把 `EXHAUSTED` 放回 `ACTIVE`（调高上限的意图就是让频道重新可用，否则还得等下个周期的定时重置）。

### 鉴权

配了 `ADMIN_API_TOKEN`（在 `.env`）则资源路由一律要求 `Authorization: Bearer <token>`，留空则全部匿名可用。`/api/health` 有意不在保护范围内，好让探针与 `make verify-*` 匿名可打。

留空是有风险的默认：`/api` 上挂着完整审计日志、频道记忆，以及**可写**的预算配额与工具白名单，而 `admin_api.host` 默认 `0.0.0.0`。公网或办公网可达时务必配上。

注意它不是登录 —— 后端只认这一个共享令牌，没有用户概念，因而无法按人区分权限或单独吊销。要那些能力得先引入会话与用户模型。

## 管理控制台

`web/` 下是一个独立的前端工程（Vite + React + TypeScript + Ant Design），消费上面那套 Admin API。需要 node ≥ 20。

```bash
make web-install    # 装依赖
make web-dev        # dev server（:5173，/api 代到本机 8000）
make web-build      # 构建到 web/dist
make web-check      # 类型检查 + 各页面渲染冒烟
```

本地开发不必配 CORS：dev server 把 `/api` 代到后端，前后端同源。后端没起也能开页面，只是各页面会显示「连不上后端」。

### 页面

顶栏切频道，侧栏走资源页。信息架构直接照 API 长 —— 十五条路径里有九条挂在 `/channels/{channel_instance_id}` 下（其余六条是 `/health`、`/channels`、`/tools`，以及按条目 id 取用的改/删记忆、单条交互记录、按任务查交互记录），所以先选频道，再选看什么。

| 页面 | 能做什么 |
|---|---|
| 频道实例 | 列出全部实例。实例由平台事件自动创建，此处只读 |
| 概览 | 身份、行为开关（主动介入 / 跨频道学习）、任务与预算摘要 |
| 任务 | 任务列表。只读 —— 状态机有合法迁移表，绕过它改会改出非法态 |
| 记忆 | 增、改、删频道记忆。错的记忆要能改或删，重要背景要能手工补。列出「产生方式」（自动蒸馏/人工写入/蒸馏后修改）与向量索引状态。写入撞上疑似重复时弹出选择界面：取代某条，还是并列写入 —— 默认不预选，这个判断只该由人做。勾「含已取代」能看到被取代的历史版本（它们不参与检索，只供排查「这条事实之前是什么」）。⚠️ 刚写入的条目「未建索引」是正常暂态（投影未追上），不是故障 |
| 预算 | 看用量、改上限与周期 |
| 权限策略 | 工具白名单（选项来自 `GET /api/tools`）、主动介入规则 |
| 标签 | 标签模板的增删与启停 |
| 审计 | 审计流水，可按动作与结果筛选，详情看 `detail` |
| 设置 | 填 Admin API 令牌 |

令牌存浏览器的 localStorage，不打进构建产物 —— 产物是静态文件，任何访客都能下载。这也意味着同机器的其他人能从 devtools 里读到它，对内网管理后台是可接受的折中。

### 前后端的对齐约束

`web/src/api/index.ts` 按资源分组，组名即 `adapters/admin/` 下的模块名；`web/src/api/types.ts` 与 `adapters/admin/serializers.py` 逐字段对应，各 union 的取值抄自 `domain/models/` 里的 Enum。

**这层对齐只能靠人守。** 后端路由的返回类型是 `dict[str, Any]`，OpenAPI schema 里没有响应形状，故字段名写错不会有编译错误，只会在页面上显示 `undefined`。改了 `serializers.py` 就得同步改 `types.ts`，反之亦然。

枚举值到中文标签的映射集中在 `web/src/components/tags.tsx`，用 `satisfies Record<X, ...>` 约束 —— 后端加了枚举值而前端漏补时会直接编译报错，而不是在页面上露出英文原文。

### 渲染冒烟

`npm run smoke`（含在 `make web-check` 里）把每个页面在 Node 里服务端渲染一遍。

`vite build` 只保证编译过，抓不到运行时问题 —— 缺失导出、循环导入、渲染期就抛的错，全都编译得过但一跑就白屏。这个探针也会把 AntD 的废弃 prop 警告打出来，跨大版本升级时尤其有用（`Drawer` 的 `width` 改 `size` 就是它报出来的）。

**它只覆盖首帧。** SSR 不执行 `useEffect`，而取数都在 `useAsync` 的 effect 里，所以数据页渲染出的是骨架屏 —— 表格、抽屉、空态这些分支一个都没走到。从字符数就能看出来：数据驱动的概览页 796 字符、策略页 635 字符，而纯静态的设置页 8608 字符。

要覆盖数据分支得换 jsdom：真实挂载 + 打桩 fetch + 等 effect 落定。那样才能验「有数据时表格渲染得出来」，代价是多一层 jsdom 依赖与 `act()` 的时序处理（嵌套的 AsyncBoundary 要多轮 tick 才稳定）。当前没做。

### 独立部署

`make web-build` 的产物是纯静态文件，托管在哪都行。与 API 不同源时要配两处，缺任一处页面都取不到数据：

```yaml
# config/config.yaml —— 放行前端来源，否则浏览器拦掉全部 /api 请求
admin_api:
  cors_origins: https://teamai-console.example.com
```

```bash
# web/.env.local —— 指向 API 的域。同源部署则留空，走相对路径 /api
VITE_API_BASE_URL=https://teamai-api.example.com
```

`deploy/nginx.conf.example` 是一份可用的托管配置，其中 `try_files ... /index.html` 这条不能省：前端用 BrowserRouter，`/channels/ch_xxx/audit` 在磁盘上没有对应文件，不回退的话用户在子路由上按刷新就是 404。首次进入不会触发（都从 `/` 跳转），只有刷新和直接贴链接会炸，因此很容易漏到上线后才发现。

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
