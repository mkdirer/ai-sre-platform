# syntax=docker/dockerfile:1.7

FROM ghcr.io/astral-sh/uv:0.12.9 AS uv
FROM python:3.12.14-slim-bookworm

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy

COPY --from=uv /uv /uvx /usr/local/bin/

RUN groupadd --gid 10001 app \
    && useradd --uid 10001 --gid app --no-create-home --shell /usr/sbin/nologin app

WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
COPY apps ./apps
COPY packages ./packages
COPY migrations ./migrations
COPY knowledge ./knowledge
COPY alembic.ini ./

RUN uv sync --locked --no-dev --no-editable --compile-bytecode

USER 10001:10001
EXPOSE 8000
