FROM ghcr.io/astral-sh/uv:0.10.2 AS uv

FROM python:3.12-slim-bookworm AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy
WORKDIR /app

COPY --from=uv /uv /uvx /bin/

COPY pyproject.toml uv.lock ./
COPY packages/research_core ./packages/research_core
COPY packages/research_connectors/pyproject.toml ./packages/research_connectors/pyproject.toml
COPY services/api ./services/api
COPY services/connector_adapter/pyproject.toml ./services/connector_adapter/pyproject.toml
COPY services/worker/pyproject.toml ./services/worker/pyproject.toml

RUN uv sync --frozen --no-dev --no-editable --package research-assistant-api

FROM python:3.12-slim-bookworm AS runtime

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8000
WORKDIR /app

RUN addgroup --system app && adduser --system --ingroup app app
COPY --from=builder --chown=app:app /app/.venv /app/.venv

USER app
EXPOSE 8000

CMD ["uvicorn", "research_assistant_api.app:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips=*"]
