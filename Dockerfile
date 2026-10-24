# One image, many roles: shop, MCP servers, API, worker, migrations.
# The compose file picks the command. Same artifact in every environment.

FROM python:3.12-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# git is needed by the local git MCP backend
RUN apt-get update \
    && apt-get install -y --no-install-recommends git curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Dependencies first so code changes do not invalidate this layer
COPY pyproject.toml README.md ./
RUN mkdir -p common shop mcp_servers opspilot evals scripts \
    && pip install ".[aws]" \
    && pip uninstall -y opspilot

COPY common ./common
COPY shop ./shop
COPY mcp_servers ./mcp_servers
COPY opspilot ./opspilot
COPY evals ./evals
COPY scripts ./scripts
COPY migrations ./migrations
COPY alembic.ini ./

# Non-root user; log directory owned by it (named volumes inherit this ownership)
RUN useradd --create-home --uid 10001 app \
    && mkdir -p /var/log/shop \
    && chown -R app:app /var/log/shop
USER app

EXPOSE 8000 8080 8101 8102 8103
CMD ["python", "-m", "opspilot.worker.main"]
