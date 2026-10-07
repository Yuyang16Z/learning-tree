# syntax=docker/dockerfile:1
# LearningTree as a container: the API has no authentication, so publish the port on
# loopback only, e.g. `docker run -p 127.0.0.1:8099:8099 -v learning-tree-data:/data ...`.

# 1. Build the web app.
FROM node:24-slim AS web
WORKDIR /src/web
COPY web/package.json web/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY web/ ./
RUN npm run build

# 2. Install locked Python dependencies (no dev tools, no optional semantic models).
FROM python:3.11-slim AS python-deps
COPY --from=ghcr.io/astral-sh/uv:0.11.25 /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# 3. Runtime image: source, built web app and dependencies; data lives in /data.
FROM python:3.11-slim
RUN useradd --system --uid 10001 --home-dir /app --no-create-home app \
    && mkdir -p /data && chown app /data
WORKDIR /app
COPY --from=python-deps /app/.venv /app/.venv
COPY app/ ./app/
COPY integrations/ ./integrations/
COPY --from=web /src/web/dist ./web/dist
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATABASE_URL=sqlite:////data/learning_tree.db
USER app
VOLUME ["/data"]
EXPOSE 8099
HEALTHCHECK --interval=5s --timeout=3s --start-period=10s --retries=5 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8099/health', timeout=2)"]
# 0.0.0.0 inside the container is required for port publishing; the Host check in
# app/main.py still accepts only loopback names (plus ALLOWED_HOSTS).
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8099"]
