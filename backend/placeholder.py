"""Генерация градиентного плейсхолдера, если обложку найти не удалось."""

from __future__ import annotations

import hashlib
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageFilter

from .config import COVERS_DIR


def _hash_rgb(text: str, salt: bytes = b"") -> tuple[int, int, int]:
    h = hashlib.sha256(salt + text.encode("utf-8")).digest()
    # Приглушённые, но насыщенные цвета под тёмную тему
    r = 40 + h[0] % 140
    g = 40 + h[1] % 140
    b = 50 + h[2] % 160
    return r, g, b


def _font(size: int) -> ImageFont.ImageFont:
    candidates = [
        "C:/Windows/Fonts/segoeui.ttf",
        "C:/Windows/Fonts/arial.ttf",
        "C:/Windows/Fonts/tahoma.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
    ]
    for p in candidates:
        if Path(p).exists():
            try:
                return ImageFont.truetype(p, size=size)
            except OSError:
                continue
    return ImageFont.load_default()


def _fit_text(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont, max_w: int) -> str:
    text = text.strip() or "—"
    if draw.textlength(text, font=font) <= max_w:
        return text
    ell = "…"
    while text and draw.textlength(text + ell, font=font) > max_w:
        text = text[:-1]
    return text + ell


def generate_placeholder(track_id: str, artist: str, title: str) -> Path:
    """Рисуем 1000×1000 PNG и кладём в cache/covers/ph_{id}.png."""
    COVERS_DIR.mkdir(parents=True, exist_ok=True)
    out = COVERS_DIR / f"ph_{track_id}.png"
    if out.exists():
        return out

    w = h = 1000
    c1 = _hash_rgb(artist + title, b"a")
    c2 = _hash_rgb(artist + title, b"b")
    img = Image.new("RGB", (w, h), c1)
    px = img.load()
    for y in range(h):
        t = y / (h - 1)
        # лёгкое смещение по диагонали
        for x in range(0, w, 4):
            u = (x / (w - 1) + t) / 2
            r = int(c1[0] * (1 - u) + c2[0] * u)
            g = int(c1[1] * (1 - u) + c2[1] * u)
            b = int(c1[2] * (1 - u) + c2[2] * u)
            for k in range(4):
                if x + k < w:
                    px[x + k, y] = (r, g, b)

    img = img.filter(ImageFilter.GaussianBlur(radius=8))
    draw = ImageDraw.Draw(img, "RGBA")

    # Виньетка
    overlay = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    od.rectangle((0, int(h * 0.55), w, h), fill=(0, 0, 0, 110))
    img = Image.alpha_composite(img.convert("RGBA"), overlay)
    draw = ImageDraw.Draw(img)

    font_title = _font(52)
    font_artist = _font(32)
    font_mark = _font(18)

    title_s = _fit_text(draw, title, font_title, w - 120)
    artist_s = _fit_text(draw, artist, font_artist, w - 120)

    draw.text((60, h - 220), artist_s, font=font_artist, fill=(255, 255, 255, 200))
    draw.text((60, h - 160), title_s, font=font_title, fill=(255, 255, 255, 245))
    draw.text((60, 50), "Курымдык  ·  нет обложки", font=font_mark, fill=(255, 255, 255, 140))

    # Декоративное кольцо — намёк на винил
    cx, cy = w - 220, 220
    draw.ellipse((cx - 90, cy - 90, cx + 90, cy + 90), outline=(255, 255, 255, 50), width=3)
    draw.ellipse((cx - 18, cy - 18, cx + 18, cy + 18), fill=(255, 255, 255, 40))

    img.convert("RGB").save(out, "PNG", optimize=True)
    return out
