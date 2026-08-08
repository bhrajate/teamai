# TeamAI 镜像：web 与 worker 共用一份，靠启动命令区分。
#
# 构建（上下文必须是仓库根，因为要 src/、app/、pyproject.toml、uv.lock）：
#   docker build -f deploy/Dockerfile -t teamai:latest .
#   或 make build
#
# 运行：
#   docker run --rm teamai:latest                          # 默认 web
#   docker run --rm teamai:latest python -m app.worker.main # worker
#
# 忽略规则在 deploy/Dockerfile.dockerignore：上下文是仓库根，而 .dockerignore
# 默认只从上下文根读取；BuildKit 支持 <dockerfile>.dockerignore，故与 Dockerfile
# 放在一起，避免往仓库根再塞一个配置文件。

# ---------- 构建阶段 ----------
FROM ghcr.io/astral-sh/uv:python3.11-bookworm-slim AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/

WORKDIR /build

# 先只拷依赖清单：这两个文件不变时，下面的 uv sync 层可命中缓存，
# 改业务代码不必重装依赖。
COPY pyproject.toml uv.lock ./

# --no-install-project：此时还没拷源码，只装第三方依赖。
# --frozen：严格按 uv.lock 装，lock 与 pyproject 不一致时直接失败，
#           不在构建期偷偷改解析结果。
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

# 再拷源码并把 teamai 自身装进 venv。
#
# --no-editable 是必须的：uv sync 默认把项目装成 editable，site-packages 里只留
# 一个 _editable_impl_teamai.pth 指向 /build/src。而运行阶段只拷 venv 不拷
# /build/src，那个路径不存在，import teamai 直接 ModuleNotFoundError。
# 加上它才会把包真正复制进 site-packages，venv 自成一体、可整体搬走。
COPY src ./src
COPY README.md* ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable

# ---------- 运行阶段 ----------
FROM python:3.11-slim-bookworm AS runtime

# 不装编译工具链：asyncpg / pydantic-core 等都有 manylinux wheel，
# 构建阶段已装好，运行阶段只需解释器。
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/.venv/bin:$PATH"

# 非 root 运行：容器逃逸时降低影响面。
RUN groupadd --gid 1000 teamai \
    && useradd --uid 1000 --gid 1000 --create-home --shell /usr/sbin/nologin teamai

WORKDIR /app

COPY --from=builder --chown=teamai:teamai /build/.venv /app/.venv

# app/ 单独拷：wheel 只打 src/teamai（见 pyproject 的 tool.hatch.build），
# 进程入口在包外，不随包安装进来。
COPY --chown=teamai:teamai app ./app

# 迁移脚本：生产建库走 alembic upgrade head（init_db 的开发建表保留给测试与本机）。
# alembic.ini 在 CWD 解析，WORKDIR=/app 故拷到根下。
COPY --chown=teamai:teamai alembic.ini ./alembic.ini
COPY --chown=teamai:teamai migrations ./migrations

# 配置文件不进镜像 —— config/config.yaml 与 .env 都含环境相关内容，
# 应在运行时挂载或用环境变量注入：
#   docker run -v ./config:/app/config:ro --env-file .env teamai:latest
# 只拷一份示例，便于进容器排查时对照字段名。
COPY --chown=teamai:teamai config/config.example.yaml ./config/config.example.yaml

USER teamai

# WORKDIR 即仓库根的等价物：config/config.yaml 与 .env 都相对 CWD 解析。
EXPOSE 8000

# 健康检查打 Admin API 的 /api/health。worker 容器没有 HTTP 端口，
# 用 --no-healthcheck 或在 compose 里覆盖。
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=2).status==200 else 1)"

# 启动前先跑迁移（幂等：alembic 按版本号跳过已应用的），再起业务进程。
# worker 容器如需同样生效，启动命令改为：
#   docker run ... /bin/sh -c "alembic upgrade head && python -m app.worker.main"
CMD ["sh", "-c", "alembic upgrade head && python -m app.backend.main"]
