FROM node:22-slim AS web
WORKDIR /web
COPY frontend/package*.json ./
RUN npm ci
COPY frontend/ ./
ARG VITE_AG_LICENSE_KEY
RUN npm run build

FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.12.15 /uv /bin/uv
WORKDIR /app/backend
COPY backend/pyproject.toml backend/uv.lock ./
RUN uv sync --frozen --no-dev
COPY backend/ ./
COPY --from=web /web/dist /app/frontend/dist
# The app runs as a plain user and only writes SQLite's files. A fresh Render disk at /var/data can mount owned by root,
# so the container starts as root, hands the directory to `app`, then drops to it before starting uvicorn.
RUN useradd --system app && mkdir -p /var/data && chown app /app/backend /var/data
CMD ["sh", "-c", "chown app /var/data && exec setpriv --reuid=app --regid=app --init-groups .venv/bin/uvicorn app:app --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers --forwarded-allow-ips '*'"]
