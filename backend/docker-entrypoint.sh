#!/bin/sh
# 啟動前建立或升級 /data 裡的 SQLite 資料庫，再啟動服務。
set -eu

mkdir -p "$(dirname "${DATABASE_URL#sqlite+aiosqlite:///}")"
alembic upgrade head
exec "$@"
