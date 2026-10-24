FROM ghcr.io/astral-sh/uv:0.12.23 AS uv
FROM python:3.12-slim AS base

COPY --from=uv /uv /usr/local/bin/uv
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    PATH="/app/.venv/bin:$PATH"
WORKDIR /app

# Resolve dependencies from the checked-in lock before copying frequently changing source.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

RUN groupadd --gid 10001 app && useradd --uid 10001 --gid app --no-create-home app
COPY --chown=app:app . .
USER app
EXPOSE 8000 8001 9001
CMD ["uvicorn", "controller.api:app", "--host", "0.0.0.0", "--port", "8000"]

FROM base AS test
USER root
RUN uv sync --frozen --no-install-project
RUN chown -R app:app /app
USER app
CMD ["pytest", "-q"]

FROM base AS runtime
