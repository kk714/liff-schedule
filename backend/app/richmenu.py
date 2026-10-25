"""建立並套用官方帳號的圖文選單（設計文件 §4.1：「我的行程」「團隊行程」「新增行程」）。

    docker exec liff-app python -m app.richmenu            # 建立、上傳圖片、設為所有使用者的預設選單
    docker exec liff-app python -m app.richmenu --dry-run  # 只印出設定

三格皆開啟 LIFF：LIFF_URL、LIFF_URL/team、LIFF_URL/new。重跑會先建立新選單，
成功設為預設後才刪除本工具先前建立的舊選單（以名稱辨識），不動其他人建立的選單。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

NAME = "liff-schedule-main"
IMAGE = Path(__file__).resolve().parent / "assets" / "richmenu.png"
WIDTH, HEIGHT = 2500, 843


def menu_definition(liff_url: str) -> dict[str, Any]:
    base = liff_url.rstrip("/")
    cell = WIDTH // 3
    targets = [("我的行程", base), ("團隊行程", f"{base}/team"), ("新增行程", f"{base}/new")]
    return {
        "size": {"width": WIDTH, "height": HEIGHT},
        "selected": True,  # 預設展開
        "name": NAME,
        "chatBarText": "行程選單",
        "areas": [
            {
                "bounds": {"x": i * cell, "y": 0, "width": cell if i < 2 else WIDTH - 2 * cell, "height": HEIGHT},
                "action": {"type": "uri", "label": label, "uri": uri},
            }
            for i, (label, uri) in enumerate(targets)
        ],
    }


class LineApi:
    def __init__(self, token: str) -> None:
        self._token = token

    def call(self, method: str, url: str, body: bytes | None = None, content_type: str = "application/json"):
        req = urllib.request.Request(url, data=body, method=method,
                                     headers={"Authorization": f"Bearer {self._token}", "Content-Type": content_type})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            raise SystemExit(f"LINE API {method} {url} failed: {exc.code} {exc.read()[:300]!r}") from exc
        return json.loads(raw) if raw else {}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m app.richmenu")
    p.add_argument("--dry-run", action="store_true")
    a = p.parse_args(argv)

    liff_url = os.environ.get("LIFF_URL", "")
    token = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
    if not liff_url:
        print("LIFF_URL is not set", file=sys.stderr)
        return 1
    definition = menu_definition(liff_url)
    if a.dry_run:
        print(json.dumps(definition, ensure_ascii=False, indent=2))
        return 0
    if not token:
        print("LINE_CHANNEL_ACCESS_TOKEN is not set", file=sys.stderr)
        return 1

    api = LineApi(token)
    old = [m["richMenuId"] for m in api.call("GET", "https://api.line.me/v2/bot/richmenu/list")["richmenus"]
           if m.get("name") == NAME]
    new_id = api.call("POST", "https://api.line.me/v2/bot/richmenu",
                      json.dumps(definition, ensure_ascii=False).encode())["richMenuId"]
    api.call("POST", f"https://api-data.line.me/v2/bot/richmenu/{new_id}/content", IMAGE.read_bytes(), "image/png")
    api.call("POST", f"https://api.line.me/v2/bot/user/all/richmenu/{new_id}")
    for menu_id in old:
        api.call("DELETE", f"https://api.line.me/v2/bot/richmenu/{menu_id}")
    print(f"rich menu {new_id} set as default; removed {len(old)} old menu(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
