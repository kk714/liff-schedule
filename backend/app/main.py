"""FastAPI 入口。

部署限制（設計文件 §12）：資料庫是單一 SQLite 檔案，服務只能跑「一個實例、一個程序」
（uvicorn --workers 1）。Worker 之後會以背景工作的形式跑在同一程序內，
工作佇列存在資料庫裡，程序重啟後未完成的工作會依租約重新領取。
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.db.engine import create_engine


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.engine = create_engine()
    yield
    await app.state.engine.dispose()


app = FastAPI(title="LINE 團隊行程助理", lifespan=lifespan)


@app.get("/healthz")
async def healthz() -> JSONResponse:
    try:
        async with app.state.engine.connect() as conn:
            version = (await conn.execute(text("SELECT version_num FROM alembic_version"))).scalar()
    except Exception as exc:  # 健康檢查只回報狀態，不中斷服務
        return JSONResponse({"status": "error", "detail": type(exc).__name__}, status_code=503)
    return JSONResponse({"status": "ok", "schema_version": version})
