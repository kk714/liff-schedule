"""FastAPI 入口與 Worker 啟動。

部署限制（設計文件 §12）：資料庫是單一 SQLite 檔案，服務只能跑「一個實例、一個程序」
（uvicorn --workers 1）。Worker 以背景工作跑在同一程序內；工作佇列存在資料庫，
程序重啟後未完成的工作會依租約重新領取。缺少 NOVARK 或 LINE 設定時只提供 API，不啟動 Worker。
"""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.chat import ConversationHandler
from app.config import Settings
from app.db.engine import create_engine, session_factory
from app.line.messaging import LineMessenger
from app.line.webhook import router as line_router
from app.llm import NovarkAdapter, NovarkSettings
from app.worker import Worker, WorkerSettings
from app.worker.memory import InProcessUserLock
from app.worker.sqlite_queue import SQLiteJobSource

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = Settings.from_env()
    engine = create_engine()
    app.state.settings = settings
    app.state.engine = engine
    app.state.sessions = session_factory(engine)

    http = httpx.AsyncClient()
    stop = asyncio.Event()
    worker_task = None
    if settings.worker_enabled and _worker_configured(settings):
        handler = ConversationHandler(
            app.state.sessions, NovarkAdapter(NovarkSettings.from_env(), http), liff_url=settings.liff_url
        )
        worker = Worker(
            SQLiteJobSource(engine), InProcessUserLock(), handler,
            LineMessenger(settings.line_channel_access_token, http),
            WorkerSettings(concurrency=settings.worker_concurrency, liff_url=settings.liff_url),
        )
        worker_task = asyncio.create_task(worker.run_forever(stop))
    elif settings.worker_enabled:
        log.warning("worker not started: NOVARK_SERVICE_TOKEN or LINE_CHANNEL_ACCESS_TOKEN missing")
    try:
        yield
    finally:
        stop.set()
        if worker_task is not None:
            await worker_task
        await http.aclose()
        await engine.dispose()


def _worker_configured(settings: Settings) -> bool:
    return bool(settings.line_channel_access_token and os.environ.get("NOVARK_SERVICE_TOKEN"))


app = FastAPI(title="LINE 團隊行程助理", lifespan=lifespan)
app.include_router(line_router)


@app.get("/healthz")
async def healthz() -> JSONResponse:
    try:
        async with app.state.engine.connect() as conn:
            version = (await conn.execute(text("SELECT version_num FROM alembic_version"))).scalar()
    except Exception as exc:  # 健康檢查只回報狀態，不中斷服務
        return JSONResponse({"status": "error", "detail": type(exc).__name__}, status_code=503)
    return JSONResponse({"status": "ok", "schema_version": version})
