"""前端靜態檔與 SPA 路由（與 API 同網域）。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    static = tmp_path / "static"
    (static / "assets").mkdir(parents=True)
    (static / "index.html").write_text("<div id=root></div>", encoding="utf-8")
    (static / "assets" / "app-abc123.js").write_text("console.log(1)", encoding="utf-8")
    (tmp_path / "secret.txt").write_text("do not serve", encoding="utf-8")
    monkeypatch.setenv("STATIC_DIR", str(static))
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'x.db'}")
    monkeypatch.setenv("WORKER_ENABLED", "false")

    from app.main import app

    with TestClient(app) as c:
        yield c


def test_frontend_routes_fall_back_to_index(client):
    for path in ("/", "/team", "/drafts/abc", "/bind?invite=x"):
        resp = client.get(path)
        assert resp.status_code == 200 and "id=root" in resp.text, path
        assert resp.headers["cache-control"] == "no-cache"


def test_hashed_assets_cached_long(client):
    resp = client.get("/assets/app-abc123.js")
    assert resp.text == "console.log(1)"
    assert "immutable" in resp.headers["cache-control"]


def test_unknown_api_path_is_json_404(client):
    resp = client.get("/api/nope")
    assert resp.status_code == 404 and resp.json() == {"error": "not_found"}


def test_api_routes_still_win(client):
    assert client.get("/api/config").json() == {"liff_id": ""}


def test_path_traversal_blocked(client):
    resp = client.get("/..%2Fsecret.txt")
    assert "do not serve" not in resp.text
