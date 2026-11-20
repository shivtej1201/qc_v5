# QC V6 — one image, several entry points (blueprint §2). Build from the repository root:
#   docker build -f v6/Dockerfile -t qc6 .
FROM node:22-bookworm-slim AS web
WORKDIR /web
COPY v6/web/package.json v6/web/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY v6/web/ ./
RUN npm run build

FROM mcr.microsoft.com/playwright/python:v1.49.0-jammy
WORKDIR /app
COPY requirements.txt /app/requirements-v5.txt
RUN pip install --no-cache-dir -r /app/requirements-v5.txt fastapi uvicorn asyncpg httpx pyyaml duckdb pyarrow pillow
COPY qcv5 /app/qcv5
COPY v6/qc6 /app/v6/qc6
COPY --from=web /web/dist /app/v6/web/dist
ENV PYTHONPATH=/app:/app/v6 QC6_DATA_DIR=/data
RUN useradd -m qc && mkdir -p /data && chown qc /data
USER qc
EXPOSE 8003
CMD ["uvicorn", "qc6.api.app:app", "--host", "0.0.0.0", "--port", "8003"]
