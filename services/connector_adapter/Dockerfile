FROM ghcr.io/astral-sh/uv:0.10.2 AS uv

FROM python:3.12-slim-bookworm AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy
WORKDIR /app

COPY --from=uv /uv /uvx /bin/

COPY pyproject.toml uv.lock ./
COPY packages/research_core ./packages/research_core
COPY packages/research_connectors ./packages/research_connectors
COPY services/connector_adapter ./services/connector_adapter
COPY services/api/pyproject.toml ./services/api/pyproject.toml
COPY services/worker/pyproject.toml ./services/worker/pyproject.toml

RUN uv sync --frozen --no-dev --no-editable --package research-assistant-connector-adapter

FROM python:3.12-slim-bookworm AS runtime

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8200
WORKDIR /app

RUN addgroup --system app && adduser --system --ingroup app app
COPY --from=builder --chown=app:app /app/.venv /app/.venv

USER app
EXPOSE 8200

CMD ["uvicorn", "research_assistant_connector_adapter.app:app", "--host", "0.0.0.0", "--port", "8200", "--proxy-headers", "--forwarded-allow-ips=*"]
