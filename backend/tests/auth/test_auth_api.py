"""登入、邀請綁定與 CLI 的整合測試（§4.2、§4.3、A09、A10、A15、A21）。

LINE 的 ID token 驗證以假的 verifier 取代；成員與邀請一律透過 CLI 建立，順便驗證 CLI。
"""

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
    """id_token 直接當成 LINE sub；"bad" 視為無效。"""

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
    monkeypatch.delenv("LIFF_URL", raising=False)

    def _run(*argv: str) -> tuple[int, str, str]:
        code = cli.main(list(argv))
        out = capsys.readouterr()
        return code, out.out, out.err

    _run("create-org", "公司")
    _run("create-member", "--org", "公司", "--name", "王小明", "--employee-code", "E001", "--admin")
    _run("create-member", "--org", "公司", "--name", "李經理", "--employee-code", "E002")
    _run("add-to-team", "--member", "E001", "--team", "業務部")
    return _run


def invite(run_cli, member="E001") -> str:
    code, out, _ = run_cli("invite", "--member", member)
    assert code == 0
    return out.strip().splitlines()[-1].removeprefix("邀請碼：")


@pytest.fixture
def client(run_cli):
    from app.main import app

    with TestClient(app, base_url="https://testserver") as c:  # Secure cookie 只會回傳給 https
        c.app.state.line_verifier = FakeVerifier()
        yield c


def csrf(client) -> dict:
    return {"x-csrf-token": client.cookies.get("csrf")}


def test_preview_shows_member_name(client, run_cli):
    resp = client.post("/api/invitations/preview", json={"token": invite(run_cli)})
    assert resp.status_code == 200
    assert resp.json()["display_name"] == "王小明" and resp.json()["organization"] == "公司"


def test_unknown_invitation_is_gone(client):
    assert client.post("/api/invitations/preview", json={"token": "nope"}).status_code == 410


def test_redeem_binds_and_starts_session(client, run_cli):
    resp = client.post("/api/invitations/redeem", json={"token": invite(run_cli), "id_token": "U-wang"})
    assert resp.status_code == 200 and resp.json()["user"]["display_name"] == "王小明"
    assert client.cookies.get("sid") and client.cookies.get("csrf")

    me = client.get("/api/me").json()
    assert me["user"]["display_name"] == "王小明" and [t["name"] for t in me["teams"]] == ["業務部"]

    _, out, _ = run_cli("list-members")
    assert "王小明" in out and "已綁定" in out and "管理員" in out


def test_a21_invitation_single_use(client, run_cli):
    token = invite(run_cli)
    assert client.post("/api/invitations/redeem", json={"token": token, "id_token": "U-wang"}).status_code == 200
    again = client.post("/api/invitations/redeem", json={"token": token, "id_token": "U-other"})
    assert again.status_code == 410


def test_new_invite_revokes_previous(client, run_cli):
    old, new = invite(run_cli), invite(run_cli)
    assert client.post("/api/invitations/preview", json={"token": old}).status_code == 410
    assert client.post("/api/invitations/preview", json={"token": new}).status_code == 200


def test_one_line_account_cannot_bind_two_members(client, run_cli):
    client.post("/api/invitations/redeem", json={"token": invite(run_cli, "E001"), "id_token": "U-same"})
    resp = client.post("/api/invitations/redeem", json={"token": invite(run_cli, "E002"), "id_token": "U-same"})
    assert resp.status_code == 409 and resp.json() == {"error": "already_bound"}


def test_bound_member_cannot_be_invited_again(run_cli, client):
    client.post("/api/invitations/redeem", json={"token": invite(run_cli), "id_token": "U-wang"})
    code, _, err = run_cli("invite", "--member", "E001")
    assert code == 1 and "revoke-binding" in err


def test_a10_invalid_id_token_rejected(client, run_cli):
    resp = client.post("/api/invitations/redeem", json={"token": invite(run_cli), "id_token": "bad"})
    assert resp.status_code == 401
    assert client.post("/api/auth/line", json={"id_token": "bad"}).status_code == 401


def test_login_bound_and_unbound(client, run_cli):
    client.post("/api/invitations/redeem", json={"token": invite(run_cli), "id_token": "U-wang"})
    client.cookies.clear()
    assert client.post("/api/auth/line", json={"id_token": "U-nobody"}).json() == {"bound": False}
    resp = client.post("/api/auth/line", json={"id_token": "U-wang"})
    assert resp.json()["bound"] is True and client.get("/api/me").status_code == 200


def test_me_requires_session(client):
    assert client.get("/api/me").status_code == 401
    client.cookies.set("sid", "forged", domain="testserver")
    assert client.get("/api/me").status_code == 401  # A09：偽造 session 無效


def test_logout_requires_csrf_then_revokes(client, run_cli):
    client.post("/api/invitations/redeem", json={"token": invite(run_cli), "id_token": "U-wang"})
    sid = client.cookies.get("sid")
    assert client.post("/api/auth/logout").status_code == 403
    assert client.post("/api/auth/logout", headers={"x-csrf-token": "wrong"}).status_code == 403
    assert client.post("/api/auth/logout", headers=csrf(client)).status_code == 200
    client.cookies.set("sid", sid, domain="testserver")  # 拿舊 session 重試
    assert client.get("/api/me").status_code == 401


def test_a15_suspension_kills_sessions_and_blocks_login(client, run_cli):
    client.post("/api/invitations/redeem", json={"token": invite(run_cli), "id_token": "U-wang"})
    assert run_cli("set-status", "--member", "E001", "--status", "suspended")[0] == 0
    assert client.get("/api/me").status_code == 401
    assert client.post("/api/auth/line", json={"id_token": "U-wang"}).status_code == 403

    run_cli("set-status", "--member", "E001", "--status", "active")  # 復職：綁定仍在
    assert client.post("/api/auth/line", json={"id_token": "U-wang"}).json()["bound"] is True


def test_revoke_binding_then_rebind_new_line_account(client, run_cli):
    client.post("/api/invitations/redeem", json={"token": invite(run_cli), "id_token": "U-old-phone"})
    code, out, _ = run_cli("revoke-binding", "--member", "E001", "--invite")
    assert code == 0 and "已解除" in out
    assert client.get("/api/me").status_code == 401
    assert client.post("/api/auth/line", json={"id_token": "U-old-phone"}).json() == {"bound": False}

    token = out.strip().splitlines()[-1].removeprefix("邀請碼：")
    assert client.post("/api/invitations/redeem", json={"token": token, "id_token": "U-new-phone"}).status_code == 200


def test_cli_errors(run_cli):
    assert run_cli("invite", "--member", "NOPE")[0] == 1
    assert run_cli("create-member", "--org", "不存在", "--name", "x")[0] == 1
    assert run_cli("remove-from-team", "--member", "E002", "--team", "業務部")[0] == 1
