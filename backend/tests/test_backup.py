from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from app.backup import backup


def make_db(path):
    con = sqlite3.connect(path)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("create table t (x)")
    con.execute("insert into t values (42)")
    con.commit()
    return con  # 保持開啟：模擬服務運作中


def test_backup_is_consistent_while_db_is_open(tmp_path):
    src = tmp_path / "app.db"
    live = make_db(src)
    dest = backup(src, tmp_path / "backups")
    live.close()
    assert sqlite3.connect(dest).execute("select x from t").fetchone() == (42,)
    assert not list((tmp_path / "backups").glob("*.partial"))


def test_keeps_only_newest(tmp_path):
    src = tmp_path / "app.db"
    make_db(src).close()
    t0 = datetime(2026, 10, 1, tzinfo=timezone.utc)
    for i in range(5):
        backup(src, tmp_path / "b", keep=3, now=t0 + timedelta(days=i))
    names = sorted(p.name for p in (tmp_path / "b").glob("app-*.db"))
    assert names == ["app-20261003-000000.db", "app-20261004-000000.db", "app-20261005-000000.db"]


def test_missing_source(tmp_path):
    with pytest.raises(FileNotFoundError):
        backup(tmp_path / "nope.db", tmp_path / "b")
