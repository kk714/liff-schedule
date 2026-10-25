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
from pathlib import Path

import httpx
from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse, Response
from sqlalchemy import text

from app.api.auth import router as auth_router
from app.api.drafts import router as drafts_router
from app.api.events import router as events_router
from app.auth.line_login import LineIdTokenVerifier
from app.chat import ConversationHandler
from app.services.errors import ServiceError
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
    app.state.line_verifier = LineIdTokenVerifier(settings.line_login_channel_id, http)
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
app.include_router(auth_router)
app.include_router(drafts_router)
app.include_router(events_router)


@app.exception_handler(ServiceError)
async def service_error(_request, exc: ServiceError) -> JSONResponse:
    # 只回錯誤代碼，不回內部訊息（§9.2）
    return JSONResponse({"error": exc.code}, status_code=exc.http_status)


@app.get("/healthz")
async def healthz() -> JSONResponse:
    try:
        async with app.state.engine.connect() as conn:
            version = (await conn.execute(text("SELECT version_num FROM alembic_version"))).scalar()
    except Exception as exc:  # 健康檢查只回報狀態，不中斷服務
        return JSONResponse({"status": "error", "detail": type(exc).__name__}, status_code=503)
    return JSONResponse({"status": "ok", "schema_version": version})


@app.get("/{path:path}", include_in_schema=False)
async def spa(path: str) -> Response:
    """LIFF 前端（與 API 同網域，§12）。存在的檔案直接回傳，其餘路徑一律回 index.html 交給前端路由。

    必須是最後一個註冊的路由，才不會蓋掉 API。
    """
    if path.startswith("api/"):
        return JSONResponse({"error": "not_found"}, status_code=404)
    root = Path(os.environ.get("STATIC_DIR", "/app/static")).resolve()
    index = root / "index.html"
    if not index.is_file():
        return JSONResponse({"error": "frontend_not_built"}, status_code=404)
    candidate = (root / path).resolve()
    if path and candidate.is_file() and candidate.is_relative_to(root):  # 擋掉 ../ 路徑穿越
        # Vite 產出的 assets 檔名含雜湊，可長期快取
        cache = "public, max-age=31536000, immutable" if path.startswith("assets/") else "no-cache"
        return FileResponse(candidate, headers={"Cache-Control": cache})
    return FileResponse(index, headers={"Cache-Control": "no-cache"})
