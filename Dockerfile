# syntax=docker/dockerfile:1
# Une seule image pour tous les processus de la Tower ; la commande de démarrage
# (tower gate | tower hangar | …) est fixée par chaque service Railway.

FROM python:3.13-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.11.32 /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /app
# Dépendances d'abord : couche mise en cache tant que uv.lock ne change pas.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY README.md LICENSE ./
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

FROM python:3.13-slim AS runtime
RUN useradd --system --uid 10001 --no-create-home tower
COPY --from=build /app/.venv /app/.venv
ENV PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    MALLOC_ARENA_MAX=2 \
    TOWER_ENV=prod
USER tower
WORKDIR /app
CMD ["tower", "gate"]
