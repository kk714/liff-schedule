"""SQLite 線上備份（設計文件 §12）。使用 SQLite backup API，服務運作中也能取得一致的快照；
不可直接複製正在寫入的資料庫檔案（WAL 模式下會得到不完整的檔案）。

    python -m app.backup --once                  # 立即備份一次
    python -m app.backup --interval-hours 24     # 每 24 小時一次（docker compose 的 backup 服務）
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

RETRY_SECONDS = 300


def source_path() -> Path:
    url = os.environ.get("DATABASE_URL", "sqlite+aiosqlite:////data/app.db")
    return Path(url.split(":///", 1)[1])


def backup(src: Path, dest_dir: Path, *, keep: int = 14, now: datetime | None = None) -> Path:
    if not src.is_file():
        raise FileNotFoundError(src)
    dest_dir.mkdir(parents=True, exist_ok=True)
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%d-%H%M%S")
    dest = dest_dir / f"app-{stamp}.db"
    tmp = dest.with_suffix(".db.partial")
    source = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    target = sqlite3.connect(tmp)
    try:
        source.backup(target)
        if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError("backup failed integrity_check")
    finally:
        target.close()
        source.close()
    tmp.replace(dest)  # 完成才改名，避免留下半份備份
    for old in sorted(dest_dir.glob("app-*.db"))[:-keep]:
        old.unlink()
    return dest


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m app.backup")
    p.add_argument("--dest", default=os.environ.get("BACKUP_DIR", "/backups"))
    p.add_argument("--keep", type=int, default=int(os.environ.get("BACKUP_KEEP", "14")))
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--once", action="store_true")
    mode.add_argument("--interval-hours", type=float)
    a = p.parse_args(argv)
    while True:
        try:
            print(f"backup written: {backup(source_path(), Path(a.dest), keep=a.keep)}", flush=True)
            delay = a.interval_hours * 3600 if not a.once else 0
        except Exception as exc:  # 備份失敗要看得到，但不能讓迴圈停掉
            print(f"backup failed: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
            if a.once:
                return 1
            delay = RETRY_SECONDS  # 失敗時不等整個週期，稍後重試
        if a.once:
            return 0
        time.sleep(delay)


if __name__ == "__main__":
    sys.exit(main())
