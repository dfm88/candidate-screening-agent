# One container serves the API and the built frontend.

FROM node:22-alpine AS frontend
WORKDIR /app/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM python:3.13-slim AS runtime
COPY --from=ghcr.io/astral-sh/uv:0.12.4 /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1
WORKDIR /app/backend
# Dependencies first, so code changes don't invalidate the dependency layer.
COPY backend/pyproject.toml backend/uv.lock backend/.python-version ./
RUN uv sync --locked --no-dev --no-install-project
COPY backend/ ./
RUN uv sync --locked --no-dev
COPY --from=frontend /app/frontend/dist /app/frontend/dist

ENV PATH="/app/backend/.venv/bin:$PATH" \
    STATIC_DIR=/app/frontend/dist \
    PORT=8080
EXPOSE 8080
# Migrations run at start: acceptable with exactly one instance (see README, "Scaling").
# Open chat streams would hold a graceful shutdown forever, and Cloud Run kills the instance 10 s after SIGTERM:
# close them after 5 s so the app's own shutdown still runs. EventSource reconnects by itself.
CMD ["sh", "-c", "alembic upgrade head && exec uvicorn screening.main:create_app --factory --host 0.0.0.0 --port ${PORT} --proxy-headers --forwarded-allow-ips='*' --timeout-graceful-shutdown 5"]
