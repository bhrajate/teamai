# TeamAI 开发与部署入口。`make` 或 `make help` 看全部目标。
#
# 分两类：
#   开发目标（install/lint/fmt/test/run-*）直接用本地 .venv 跑，快，不碰 docker
#   容器目标（up/down/build/...）管依赖服务与镜像
#
# 所有目标都从仓库根执行：config/config.yaml 与 .env 相对 CWD 解析。

VENV       := .venv
PY         := $(VENV)/bin/python
# 控制台前端。独立的 node 工程，与 python 侧互不干扰
WEB        := web
NPM        := npm --prefix $(WEB)
PYTEST     := $(VENV)/bin/pytest
RUFF       := $(VENV)/bin/ruff
UV_INDEX   := https://mirrors.aliyun.com/pypi/simple/
UV         := UV_INDEX_URL=$(UV_INDEX) uv
# --env-file 必须显式给：compose 的 project directory 默认取 compose 文件所在目录，
# 它找的是 deploy/.env，而本项目的 .env 在仓库根。不指定则 compose 里的
# ${POSTGRES_PORT:-5432} 之类永远只取默认值，改了 .env 也不生效。
# --project-directory 一并钉到仓库根，保证相对路径的解释一致。
COMPOSE    := docker compose --env-file .env --project-directory . -f deploy/docker-compose.yml
IMAGE      ?= teamai:latest

# 目标名与同名目录/文件冲突时（如 config、tests），make 会以为已是最新而跳过
.PHONY: help install lock sync lint fmt test test-cov check run-web run-worker migrate \
	verify-longtask verify-longtask-db verify-outbox \
	web-install web-dev web-build web-check \
        up down restart logs ps build image-run clean config

.DEFAULT_GOAL := help

help:  ## 列出全部目标
	@grep -hE '^[a-zA-Z0-9_-]+:.*?##' $(MAKEFILE_LIST) \
		| sort \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

# ---------- 环境 ----------

install:  ## 建 venv 并装全部依赖（含 dev）
	$(UV) sync --extra dev

lock:  ## 按 pyproject 重新解析并更新 uv.lock
	$(UV) lock

sync:  ## 严格按 uv.lock 同步依赖（不改 lock）
	$(UV) sync --frozen --extra dev

config:  ## 从示例生成 config/config.yaml 与 .env（已存在则不覆盖）
	@test -f config/config.yaml \
		&& echo "config/config.yaml 已存在，跳过" \
		|| (cp config/config.example.yaml config/config.yaml && echo "已生成 config/config.yaml")
	@test -f .env \
		&& echo ".env 已存在，跳过" \
		|| (cp .env.example .env && echo "已生成 .env，记得填 token")

# ---------- 代码质量 ----------

lint:  ## ruff 检查（不改文件）
	$(RUFF) check src tests app scripts

fmt:  ## ruff 自动修复可修项
	$(RUFF) check --fix src tests app scripts

test:  ## 跑全部测试
	$(PYTEST) -q

test-cov:  ## 跑测试并输出覆盖率（需先装 pytest-cov）
	$(PYTEST) --cov=src/teamai --cov-report=term-missing

check: lint test  ## lint + test，提交前跑这个

# ---------- 本地运行 ----------

run-web:  ## 起 web 进程（Admin API + Slack 入口）
	$(PY) -m app.backend.main

run-worker:  ## 起 worker 进程（消费队列 + 定时调度）
	$(PY) -m app.worker.main

verify-longtask:  ## 冒烟验证长任务链路（需 make up 起 redis）
	$(PY) -m scripts.verify_long_task_flow

verify-longtask-db:  ## 同上但用真 Container 入队，随后手动跑 make run-worker 看消费
	$(PY) -m scripts.verify_long_task_flow --real-db

verify-outbox:  ## 冒烟验证记忆投影链路：写入 → 入队 → 投影 → 回填（需 postgres + make migrate）
	$(PY) -m scripts.verify_outbox_flow

# ---------- 控制台前端 ----------
# 需要 node ≥ 20。前端是独立静态站，构建产物在 web/dist/。

web-install:  ## 装前端依赖
	$(NPM) install

web-dev:  ## 起前端 dev server（:5173，/api 代到本机 8000，无须配 CORS）
	$(NPM) run dev

web-build:  ## 构建前端到 web/dist（先跑 tsc 类型检查）
	$(NPM) run build

web-check:  ## 前端类型检查 + 各页面渲染冒烟（构建抓不到运行时错误）
	$(NPM) run check

migrate:  ## 应用数据库迁移（alembic upgrade head，生产建库路径）
	$(UV) run alembic upgrade head

# ---------- 依赖服务 ----------

up:  ## 后台起依赖容器（postgres + redis + qdrant）
	$(COMPOSE) up -d

down:  ## 停依赖容器（保留数据卷）
	$(COMPOSE) down

restart: down up  ## 重启依赖容器

logs:  ## 跟看依赖容器日志
	$(COMPOSE) logs -f

ps:  ## 依赖容器状态
	$(COMPOSE) ps

# ---------- 镜像 ----------

build:  ## 构建应用镜像（上下文为仓库根）
	docker build -f deploy/Dockerfile -t $(IMAGE) .

image-run:  ## 用镜像起 web，挂载本机 config 与 .env
	docker run --rm -p 8000:8000 \
		-v $(CURDIR)/config:/app/config:ro \
		$$(test -f .env && echo "--env-file .env") \
		$(IMAGE)

# ---------- 清理 ----------

clean:  ## 删缓存与构建产物（不动 .venv 与数据卷）
	find . -type d -name __pycache__ -not -path "./.venv/*" -exec rm -rf {} + 2>/dev/null || true
	rm -rf .pytest_cache .ruff_cache .mypy_cache dist build
	@echo "已清理缓存"
