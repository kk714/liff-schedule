#!/usr/bin/env bash
# 在本機建置 image，經 SSH 傳到 GCE 載入並以 docker compose 啟動。
#   SSH_KEY=/d/ssh-kyle ./deploy/deploy.sh
# 伺服器上的 .env（含機密）不在 git 內，首次部署前需手動建立：/opt/liff-schedule/.env（權限 600）。
set -euo pipefail

HOST="${HOST:-kyle@104.199.191.170}"
SSH_KEY="${SSH_KEY:-$HOME/.ssh/id_ed25519}"
REMOTE_DIR="${REMOTE_DIR:-/opt/liff-schedule}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TAG="$(git -C "$ROOT" rev-parse --short HEAD)$(git -C "$ROOT" diff --quiet || echo -dirty)"
SSH=(ssh -i "$SSH_KEY" -o BatchMode=yes "$HOST")

if [ "${SKIP_TESTS:-0}" != "1" ]; then
  # 測試沒過就不部署（曾因語法錯誤直接部署導致服務重啟循環）
  echo "==> tests"
  PY="$ROOT/backend/.venv/Scripts/python"; [ -x "$PY" ] || PY="$ROOT/backend/.venv/bin/python"
  (cd "$ROOT/backend" && "$PY" -m pytest -q -x) || { echo "backend tests failed; not deploying" >&2; exit 1; }
  (cd "$ROOT/frontend" && npx vitest run --silent) || { echo "frontend tests failed; not deploying" >&2; exit 1; }
fi

echo "==> build liff-schedule:$TAG"
docker build -t "liff-schedule:$TAG" -t liff-schedule:latest "$ROOT"

echo "==> upload image"
docker save "liff-schedule:$TAG" | gzip | "${SSH[@]}" "gunzip | sudo docker load && sudo docker tag liff-schedule:$TAG liff-schedule:latest"
# 伺服器上的 latest 永遠指向最後部署的版本，手動 docker compose up 時才不會去 registry 拉

echo "==> upload compose files"
tar -C "$ROOT/deploy" -cf - docker-compose.yml nginx | "${SSH[@]}" "sudo tar -C $REMOTE_DIR -xf -"

echo "==> start"
"${SSH[@]}" "cd $REMOTE_DIR && test -f .env || { echo 'missing $REMOTE_DIR/.env' >&2; exit 1; }
  sudo mkdir -p backups && sudo chown 10001 backups
  sudo TAG=$TAG docker compose up -d --remove-orphans
  sudo docker compose ps"

echo "==> health"
"${SSH[@]}" "for i in \$(seq 1 30); do sudo docker exec liff-app python -c \"import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:8000/healthz').read().decode())\" 2>/dev/null && exit 0; sleep 2; done; exit 1"
