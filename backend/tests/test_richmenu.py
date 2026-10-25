from __future__ import annotations

from PIL import Image  # noqa: F401  只有開發環境有 Pillow；用來確認圖片尺寸

from app.richmenu import HEIGHT, IMAGE, WIDTH, menu_definition


def test_three_areas_cover_the_menu_and_open_liff_paths():
    menu = menu_definition("https://liff.line.me/2012197543-bgyJ5YbJ/")
    areas = menu["areas"]
    assert [a["action"]["uri"] for a in areas] == [
        "https://liff.line.me/2012197543-bgyJ5YbJ",
        "https://liff.line.me/2012197543-bgyJ5YbJ/team",
        "https://liff.line.me/2012197543-bgyJ5YbJ/new",
    ]
    assert sum(a["bounds"]["width"] for a in areas) == WIDTH
    assert all(a["bounds"]["height"] == HEIGHT for a in areas)
    assert len(menu["chatBarText"]) <= 14  # LINE 限制


def test_image_matches_menu_size_and_limit():
    with Image.open(IMAGE) as img:
        assert img.size == (WIDTH, HEIGHT)
    assert IMAGE.stat().st_size < 1024 * 1024  # LINE 上限 1 MB
