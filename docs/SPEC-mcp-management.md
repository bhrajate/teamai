# SPEC：MCP 管理功能

> 状态：设计中（待评审）
> 日期：2026-08-20
> 范围：前端（React 管理控制台）+ 后端（FastAPI admin API + worker 挂载），首期只做设计落定后开工

## 1. 背景与目标

teamai 目前只有三个内置工具（github / monitoring / crm），工具集固定在启动时注册
（`container.py:build_tools()`）。TODO 里的 mcp 是 roadmap 未实现项。

**目标**：让管理员在管理控制台为每个频道配置 MCP server（streamable HTTP），
MCP server 的工具以 server 级挂载进入该频道现有的工具白名单机制，agent 在频道里
即可调用。

**非目标（首期明确不做）**：stdio/SSE 传输、按用户级配置、热加载、MCP 资源与
prompts 暴露、OAuth 凭据流程。

## 2. 需求决策（用户已拍板）

| 决策点 | 结论 | 理由 |
|---|---|---|
| 配置作用域 | **按频道配置** | 频道是现有配置单位（policy/预算均按 `channel_instance_id`），贴合团队共享 AI 形态 |
| 传输方式 | **streamable HTTP** | 无需管理子进程，Docker 部署友好；pydantic-ai 2.25 内置 `StreamableHttpTransport` |
| 挂载粒度 | **server 级开关** | 白名单按 `mcp__<server>` 挂载，无需预知动态工具名；精确工具名匹配机制顺带支持但不做 UI |
| 生效方式 | **重启生效** | 配置落库，worker 启动时从 DB 加载连接注册；与现有静态注册模式一致 |

## 3. 领域设计

### 3.1 领域模型：McpServer（新增 `domain/models/mcp.py`）

```
McpServer:
  id: str(32)                 # gen_id("mcp")
  channel_instance_id: str(32)
  name: str                   # 短名，约束 ^[a-z0-9-]+$，同频道内唯一
  url: str                    # streamable HTTP 端点
  headers: dict[str, str]     # 认证等自定义头
  enabled: bool
  last_error: str | None      # 最近一次连接失败原因（启动快照，非实时探活）
  created_at / updated_at: datetime
```

**工具命名约定**：MCP 工具注册名为 `mcp__<server_name>__<tool_name>`
（与 pydantic-ai MCP 客户端默认、Claude Desktop 惯例一致）。
name 的字符约束就是为了保证这个名字可解析、可进白名单。

### 3.2 白名单挂载机制（改动 `infrastructure/tools/registry.py`）

`ToolRegistry.for_channel(allowed)` 现有逻辑是精确名匹配。扩展规则：

- 白名单条目形如 `mcp__<server>`（恰好两段，不含第二个 `__`）→ **server 级挂载**，
  展开为该 server 注册的全部工具
- 白名单条目是完整工具名 `mcp__<server>__<tool>` → 精确挂载单个工具（机制顺带支持）
- 未注册名字照旧忽略（server 连接失败导致工具缺失时，策略残留不报错）

**连接失败行为**：MCP server 连接失败（URL 错、服务未起）时启动不崩，该 server
的工具不注册，`last_error` 落库。若白名单仍挂着它 → 工具集为空被 `for_channel`
自然忽略，与「策略里残留已下线工具名」的现有语义一致（`registry.py:62` 注释）。

## 4. 后端设计

### 4.1 数据模型（ORM + 迁移）

- `infrastructure/orm/mcp.py`：`McpServerModel`，表 `mcp_servers`
  - `headers` 存 JSON 字符串 Text 列（对齐 `PolicyModel.allowed_tools` 的先例，
    序列化在 repository mapper 里完成）
  - `(channel_instance_id, name)` 唯一约束
- `migrations/versions/<rev>_mcp_servers_建表.py`：alembic 迁移

### 4.2 Repository（对齐现有模式）

- `domain/repositories/mcp.py`：`McpServerRepository` 接口
  - `list_for_channel(id)` / `list_enabled()`（worker 加载用）
  - `get(channel_id, server_id)` / `upsert(McpServer)` / `delete(channel_id, server_id)`
- `infrastructure/repositories/mcp.py`：SQLAlchemy 实现，JSON 编解码在 mapper

### 4.3 MCP 客户端封装（新增 `infrastructure/mcp/`）

- `client.py`：包一层 pydantic-ai 的 `StreamableHttpTransport` + `MCPToolsetClient`
  - `connect(url, headers)` → 握手（initialize），返回工具名列表
  - 失败抛统一 `McpConnectionError`（含握手错误详情）
- 测试用 `FastMCP` 本地起 server（见 §6），不依赖外部真实 server

### 4.4 启动挂载（worker 侧）

- `application/mcp.py`：`McpService`
  - `load_and_register(registry)`：读 `list_enabled()` → 逐个 `connect` →
    工具以 `mcp__<server>__<tool>` 注册进 `ToolRegistry`；失败记 `last_error` + warn 日志
  - `test_connection(url, headers)`：供 admin test 端点复用（纯握手 + 返回工具名）
- 调用点：worker 启动的 async 引导处（`app/worker/main.py` 构建 container 后、
  首个任务调度前）。`container.build_tools()` 保持同步注册内置工具不变，
  MCP 注册作为独立的 async 步骤补入

### 4.5 Admin API（新增 `adapters/admin/mcp.py`）

`build_mcp_router(container)`，路径前缀 `/channels/{channel_instance_id}/mcp-servers`：

| 方法 | 路径 | 行为 |
|---|---|---|
| GET | `/` | 列表（headers **脱敏**回显 `***`，含 last_error） |
| POST | `/` | 创建（name 字符校验 + 同频道唯一校验，409 报错） |
| PUT | `/{server_id}` | 更新（可只改 enabled 做启停） |
| DELETE | `/{server_id}` | 删除 |
| POST | `/test` | 连接测试：body `{url, headers}`，握手成功返回工具名列表；失败 422 带错误详情 |

**敏感信息策略**：headers 明文存 DB（内部部署信任模型，与现有管理台 token
存 localStorage 同级）；API 响应一律脱敏，防止凭据经前端往返泄露。
**SSRF 边界**：test 端点接受任意 URL —— 管理控制台本身有 token 鉴权，
管理员等价于受信方，风险接受（SPEC 备案）。

### 4.6 Serializer

`adapters/admin/serializers.py` 加 `mcp_server_to_dict`（含脱敏），
`web/src/api/types.ts` 的 `McpServer` 与之逐字段对齐（该对齐靠人工守，见 types.ts 头注释）。

## 5. 前端设计（React + antd，对齐现有模式）

### 5.1 路由与页面

- 新路由 `/channels/:channelId/mcp` → `routes/McpServerPage.tsx`
- 频道内导航入口（对齐现有频道页面入口方式）
- 页面内容：
  - 表格：名称 / URL / 启用开关（Switch，就地启停）/ 最近错误（last_error，tooltip 展示）/ 操作（编辑、删除）
  - 新增 & 编辑：Modal 表单 —— name（只读在编辑态）、url、headers 键值对列表（动态增删行）
  - **测试连接**按钮：表单内输入 url+headers 后即测，成功展示工具数/工具名列表，失败展示错误
- 删除用 Popconfirm 二次确认；启用/停用即保存（对齐 BudgetPage 就地交互风格）

### 5.2 API 层

- `web/src/api/index.ts` 加 `mcpApi`（list/create/update/delete/test），
  路径对齐后端；`types.ts` 加 `McpServer`、`McpTestResult`
- 错误处理复用 `ApiError` 模式（`api/client.ts`）

## 6. 测试策略

| 层 | 方式 | 对齐先例 |
|---|---|---|
| client 封装 | 单元测试：`FastMCP` 起内存 server，通过 httpx ASGI transport 连（无端口），验证握手与工具名解析；断连/握手失败路径 | tests/ 下新增 test_mcp_client.py |
| repository | SQLite 内存真 SQL 行为测试（唯一约束、JSON 编解码、CRUD） | 现有仓储测试模式 |
| admin API | 依赖注入 fake repo（tests/doubles.py 模式），验证路由、脱敏、name 校验、409 | 现有 admin 路由测试 |
| 白名单扩展 | 单元测试 `for_channel`：server 级前缀展开、精确工具名、未注册忽略 | tests/ 现有 registry 测试 |
| 前端 | `npm run check`（tsc + smoke 渲染），类型对齐靠 types.ts 注释纪律 | 现有 |

## 7. 边界与已知限制（首期）

- **只做 streamable HTTP**；stdio / SSE 传输后续再加
- **重启生效**；配置变更后需重启 worker。`last_error` 是启动快照，不实时探活
- **按频道配置**；不做用户级、不做全局共享配置
- **只暴露工具**；MCP 的 resources / prompts 不暴露给 agent
- **headers 手填**；不做 OAuth 认证流程（如 GitHub 的 OAuth 授权）
- 工具级精确挂载机制顺带支持但 UI 不暴露（白名单仍可在 policy 页手填完整工具名）
- 一个 channel 同时挂多个 MCP server 支持；同一 server 配置在多个频道下是不同行（按频道存储）

## 8. 验收标准

1. 管理员在控制台为某频道新增 MCP server（填 URL + headers），测试连接成功能看到工具名列表
2. 在策略页该频道的 `allowed_tools` 勾选/填入 `mcp__<server>` 后，agent 可调用该 server 的工具
3. 频道内的 agent run 中，MCP 工具调用走现有 `_GracefulToolset` 错误收口（缺工具不炸 run）
4. MCP server 不可达时：worker 启动不崩、`last_error` 有值、前端可见
5. 凭据不出现在任何 API 响应中（脱敏回显）
6. 删除/停用 server 后，白名单残留条目不影响 agent（机制自然忽略）
7. 前后端测试全绿：`uv run pytest` + `npm run check`

## 9. 实施顺序（规划，实施时再拆任务）

1. domain model + ORM + migration + repository（含测试）
2. `infrastructure/mcp/client.py` + 单元测试（FastMCP 内存 server）
3. `ToolRegistry.for_channel` 白名单扩展 + 测试
4. `application/mcp.py` McpService + worker 启动挂载
5. `adapters/admin/mcp.py` 路由 + serializer + 测试
6. 前端 `McpServerPage` + api 层 + 路由注册
7. 全量验证（验收标准逐条过）
