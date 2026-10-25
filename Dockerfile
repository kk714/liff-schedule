# LINE 團隊行程助理：前端（LIFF）＋後端 API＋Worker，單一 image、單一程序。
# 資料庫為 SQLite，檔案放在 /data（必須掛載 volume，否則重新部署資料會消失）。
#   docker build -t liff-schedule .
#   docker run -d -p 8000:8000 -v liff-data:/data --env-file backend/.env liff-schedule

FROM node:22-slim AS web
WORKDIR /web
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATABASE_URL=sqlite+aiosqlite:////data/app.db \
    STATIC_DIR=/app/static

WORKDIR /app
COPY backend/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/alembic.ini backend/docker-entrypoint.sh ./
COPY backend/migrations ./migrations
COPY backend/app ./app
COPY --from=web /web/dist ./static
RUN sed -i 's/\r$//' docker-entrypoint.sh && chmod +x docker-entrypoint.sh \
    && useradd --uid 10001 --no-create-home app && mkdir -p /data && chown app /data

USER app
VOLUME ["/data"]
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4)"

ENTRYPOINT ["./docker-entrypoint.sh"]
# SQLite 單一檔案：只能一個程序（--workers 1），也只能部署一個實例
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--proxy-headers"]
