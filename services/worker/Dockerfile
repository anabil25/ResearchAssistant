FROM ghcr.io/astral-sh/uv:0.10.2 AS uv

FROM python:3.12-slim-bookworm AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy
WORKDIR /app

COPY --from=uv /uv /uvx /bin/

COPY pyproject.toml uv.lock ./
COPY packages/research_core ./packages/research_core
COPY packages/research_connectors/pyproject.toml ./packages/research_connectors/pyproject.toml
COPY services/worker ./services/worker
COPY services/api/pyproject.toml ./services/api/pyproject.toml

RUN uv sync --frozen --no-dev --no-editable --package research-assistant-worker

FROM python:3.12-slim-bookworm AS runtime

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
WORKDIR /app

RUN addgroup --system app && adduser --system --ingroup app app
COPY --from=builder --chown=app:app /app/.venv /app/.venv

USER app

CMD ["python", "-m", "research_assistant_worker"]
