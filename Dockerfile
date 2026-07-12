FROM ghcr.io/astral-sh/uv:0.10.7 AS uv

FROM python:3.13-slim

COPY --from=uv /uv /uvx /bin/
WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src ./src
RUN uv sync --frozen --no-dev

RUN useradd --create-home --uid 10001 app && chmod -R a+rX /app && mkdir -p /data/exports && chown -R app:app /data
USER app

VOLUME ["/data"]
EXPOSE 8000

CMD ["/app/.venv/bin/uvicorn", "--factory", "health_export_api.app:create_app_from_env", "--host", "0.0.0.0", "--port", "8000"]
