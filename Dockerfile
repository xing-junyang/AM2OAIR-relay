FROM node:24-alpine AS frontend-builder
WORKDIR /build
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && groupadd --gid 10001 relay \
    && useradd --uid 10001 --gid 10001 --no-create-home relay \
    && mkdir -p /data \
    && chown relay:relay /data
COPY relay/ ./relay/
COPY scripts/db_admin.py ./scripts/db_admin.py
COPY --from=frontend-builder /build/dist ./frontend/dist
USER 10001:10001
EXPOSE 8787
HEALTHCHECK --interval=15s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8787/healthz', timeout=4)"
CMD ["uvicorn", "relay.app:app", "--host", "0.0.0.0", "--port", "8787", "--no-access-log"]
