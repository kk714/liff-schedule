"""產生 LINE 圖文選單圖片（2500×843，三格）。只在開發機執行，需要 Pillow 與微軟正黑體。

    backend/.venv/Scripts/python deploy/make_richmenu_image.py

輸出到 backend/app/assets/richmenu.png（會隨 image 部署，供 python -m app.richmenu 上傳）。
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

W, H = 2500, 843
OUT = Path(__file__).resolve().parents[1] / "backend" / "app" / "assets" / "richmenu.png"
FONT = "C:/Windows/Fonts/msjhbd.ttc"
GREEN, DARK, WHITE = (6, 199, 85), (4, 140, 60), (255, 255, 255)
CELLS = [
    ("我的行程", "今天・本週・本月", "list"),
    ("團隊行程", "一天一天看大家的空檔", "grid"),
    ("新增行程", "或直接在聊天室打字", "plus"),
]


def icon(d: ImageDraw.ImageDraw, kind: str, cx: int, cy: int, s: int) -> None:
    w = 16
    if kind == "list":
        d.rounded_rectangle([cx - s, cy - s, cx + s, cy + s], radius=24, outline=WHITE, width=w)
        d.rectangle([cx - s, cy - s, cx + s, cy - s // 2], fill=WHITE)
        for i, y in enumerate((cy + 0, cy + s // 2)):
            d.line([cx - s + 40, y, cx + s - 40, y], fill=WHITE, width=w)
    elif kind == "grid":
        d.rounded_rectangle([cx - s, cy - s, cx + s, cy + s], radius=24, outline=WHITE, width=w)
        for x in (cx - s // 3, cx + s // 3):
            d.line([x, cy - s, x, cy + s], fill=WHITE, width=w // 2)
        for y in (cy - s // 3, cy + s // 3):
            d.line([cx - s, y, cx + s, y], fill=WHITE, width=w // 2)
        d.rectangle([cx - s // 3 + 8, cy - s // 3 + 8, cx + s // 3 - 8, cy + s // 3 - 8], fill=WHITE)
    else:
        d.ellipse([cx - s, cy - s, cx + s, cy + s], outline=WHITE, width=w)
        d.line([cx - s // 2, cy, cx + s // 2, cy], fill=WHITE, width=w + 4)
        d.line([cx, cy - s // 2, cx, cy + s // 2], fill=WHITE, width=w + 4)


def main() -> None:
    img = Image.new("RGB", (W, H), GREEN)
    d = ImageDraw.Draw(img)
    title_font = ImageFont.truetype(FONT, 120)
    sub_font = ImageFont.truetype(FONT, 56)
    cell_w = W // 3
    for i, (title, sub, kind) in enumerate(CELLS):
        x0 = i * cell_w
        if i:
            d.line([x0, 90, x0, H - 90], fill=DARK, width=6)
        cx = x0 + cell_w // 2
        icon(d, kind, cx, 250, 110)
        d.text((cx, 520), title, font=title_font, fill=WHITE, anchor="mm")
        d.text((cx, 650), sub, font=sub_font, fill=(225, 255, 235), anchor="mm")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    img.save(OUT, optimize=True)
    print(f"{OUT} ({OUT.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()
