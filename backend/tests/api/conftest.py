from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient

from app import cli
from app.auth.line_login import InvalidIdToken, LineClaims

BACKEND = Path(__file__).resolve().parents[2]


class FakeVerifier:
    async def verify(self, id_token: str) -> LineClaims:
        if id_token == "bad":
            raise InvalidIdToken("bad")
        return LineClaims(sub=id_token)


@pytest.fixture
def run_cli(tmp_path, monkeypatch, capsys):
    db = tmp_path / "app.db"
    cfg = Config(str(BACKEND / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND / "migrations"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db}")
    command.upgrade(cfg, "head")
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db}")
    monkeypatch.setenv("WORKER_ENABLED", "false")
    monkeypatch.setenv("LIFF_ID", "1650000000-test")
    monkeypatch.delenv("LIFF_URL", raising=False)

    def _run(*argv: str) -> str:
        assert cli.main(list(argv)) == 0, argv
        return capsys.readouterr().out

    _run("create-org", "公司")
    for code, name in (("E001", "我"), ("E002", "王小明"), ("E003", "陌生同事")):
        _run("create-member", "--org", "公司", "--name", name, "--employee-code", code)
    _run("add-to-team", "--member", "E001", "--team", "業務部")
    _run("add-to-team", "--member", "E002", "--team", "業務部")
    _run("add-to-team", "--member", "E003", "--team", "研發部")
    return _run


def _login(app, run_cli, code: str, sub: str) -> TestClient:
    c = TestClient(app, base_url="https://testserver")
    c.__enter__()
    c.app.state.line_verifier = FakeVerifier()
    token = run_cli("invite", "--member", code).strip().splitlines()[-1].removeprefix("邀請碼：")
    assert c.post("/api/invitations/redeem", json={"token": token, "id_token": sub}).status_code == 200
    c.headers["x-csrf-token"] = c.cookies.get("csrf")
    return c


@pytest.fixture
def clients(run_cli):
    """(我, 同團隊的王小明, 無共同團隊的陌生同事, 未登入)，各自獨立的 cookie。"""
    from app.main import app

    me = _login(app, run_cli, "E001", "U-me")
    mate = _login(app, run_cli, "E002", "U-mate")
    stranger = _login(app, run_cli, "E003", "U-stranger")
    anon = TestClient(app, base_url="https://testserver")
    anon.__enter__()
    yield me, mate, stranger, anon
    for c in (me, mate, stranger, anon):
        c.__exit__(None, None, None)
