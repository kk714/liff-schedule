from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient

BACKEND = Path(__file__).resolve().parents[1]


def test_healthz_reports_schema_version(tmp_path, monkeypatch):
    db = tmp_path / "app.db"
    cfg = Config(str(BACKEND / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND / "migrations"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db}")
    command.upgrade(cfg, "head")
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db}")

    from app.main import app

    with TestClient(app) as client:
        resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "schema_version": "0002"}


def test_healthz_unavailable_without_schema(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'empty.db'}")

    from app.main import app

    with TestClient(app) as client:
        resp = client.get("/healthz")
    assert resp.status_code == 503
