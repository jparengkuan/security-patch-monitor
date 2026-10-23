"""Generate the app icon (calendar page with a security shield) and all derived sizes.

Usage (requires Pillow and, for the .icns, macOS ``sips`` + ``iconutil``):

    python tools/make_icon.py
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

ROOT = Path(__file__).resolve().parent.parent
MASTER = ROOT / "packaging" / "icon-1024.png"
ICNS = ROOT / "packaging" / "macos" / "AppIcon.icns"
STATIC = ROOT / "src" / "patch_monitor" / "web" / "static"

SCALE = 2048  # drawn supersampled, downscaled to 1024
WHITE = (255, 255, 255, 255)
RED = (229, 57, 53, 255)
PAGE = (246, 248, 252, 255)
DOTS = (214, 220, 232, 255)
RINGS = (226, 232, 244, 255)
SHIELD = (45, 91, 215, 255)
SHIELD_INNER = (60, 110, 235, 255)
GRADIENT = ((37, 64, 160), (16, 28, 74))


def draw_icon() -> Image.Image:
    img = Image.new("RGBA", (SCALE, SCALE), (0, 0, 0, 0))
    _draw_background(img)
    draw = ImageDraw.Draw(img)
    _draw_calendar(draw)
    _draw_shield(img)
    return img.resize((1024, 1024), Image.Resampling.LANCZOS)


def _draw_background(img: Image.Image) -> None:
    margin, radius = 200, 370
    top, bottom = GRADIENT
    gradient = Image.new("RGBA", (SCALE, SCALE))
    gd = ImageDraw.Draw(gradient)
    for y in range(SCALE):
        t = y / SCALE
        colour = tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3))
        gd.line([(0, y), (SCALE, y)], fill=(*colour, 255))
    mask = Image.new("L", (SCALE, SCALE), 0)
    ImageDraw.Draw(mask).rounded_rectangle([margin, margin, SCALE - margin, SCALE - margin], radius, fill=255)
    shadow = Image.new("RGBA", (SCALE, SCALE), (0, 0, 0, 0))
    ImageDraw.Draw(shadow).rounded_rectangle(
        [margin, margin + 30, SCALE - margin, SCALE - margin + 30], radius, fill=(0, 0, 0, 110)
    )
    img.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(40)))
    img.paste(gradient, (0, 0), mask)


def _draw_calendar(draw: ImageDraw.ImageDraw) -> None:
    x0, y0, x1, y1 = 520, 560, 1528, 1560
    draw.rounded_rectangle([x0, y0 + 20, x1, y1 + 20], 110, fill=(0, 0, 0, 70))
    draw.rounded_rectangle([x0, y0, x1, y1], 110, fill=PAGE)
    draw.rounded_rectangle([x0, y0, x1, y0 + 260], 110, fill=RED)
    draw.rectangle([x0, y0 + 150, x1, y0 + 260], fill=RED)
    for x in (760, 1288):  # binder rings
        draw.rounded_rectangle([x - 42, y0 - 110, x + 42, y0 + 90], 42, fill=RINGS)
    for row in range(3):
        for col in range(5):
            x, y = x0 + 150 + col * 177, y0 + 400 + row * 245
            draw.rounded_rectangle([x - 40, y - 40, x + 40, y + 40], 18, fill=DOTS)


def _draw_shield(img: Image.Image) -> None:
    cx, cy, w, h = 1024, 1080, 520, 600
    outline = [
        (cx, cy - h // 2),
        (cx + w // 2, cy - h // 2 + 110),
        (cx + w // 2, cy + 20),
        (cx + w // 2 - 40, cy + 150),
        (cx, cy + h // 2 + 40),
        (cx - w // 2 + 40, cy + 150),
        (cx - w // 2, cy + 20),
        (cx - w // 2, cy - h // 2 + 110),
    ]
    shadow = Image.new("RGBA", (SCALE, SCALE), (0, 0, 0, 0))
    ImageDraw.Draw(shadow).polygon([(x, y + 26) for x, y in outline], fill=(0, 0, 0, 90))
    img.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(18)))

    draw = ImageDraw.Draw(img)
    draw.polygon(outline, fill=SHIELD)
    draw.polygon([(cx + (x - cx) * 0.84, cy + 10 + (y - cy) * 0.84) for x, y in outline], fill=SHIELD_INNER)
    start, corner, end = (cx - 140, cy + 20), (cx - 30, cy + 140), (cx + 170, cy - 110)
    draw.line([start, corner, end], fill=WHITE, width=78, joint="curve")
    for px, py in (start, end):
        draw.ellipse([px - 39, py - 39, px + 39, py + 39], fill=WHITE)


def write_sizes(master: Image.Image) -> None:
    MASTER.parent.mkdir(parents=True, exist_ok=True)
    master.save(MASTER)
    master.resize((512, 512), Image.Resampling.LANCZOS).save(STATIC / "icon-512.png")
    master.resize((64, 64), Image.Resampling.LANCZOS).save(STATIC / "icon.png")


def write_icns() -> None:
    if not (shutil.which("sips") and shutil.which("iconutil")):
        print("Skipping .icns (sips/iconutil not available)")
        return
    with tempfile.TemporaryDirectory() as tmp:
        iconset = Path(tmp) / "AppIcon.iconset"
        iconset.mkdir()
        for size in (16, 32, 128, 256, 512):
            for factor, suffix in ((1, ""), (2, "@2x")):
                out = iconset / f"icon_{size}x{size}{suffix}.png"
                px = str(size * factor)
                subprocess.run(["sips", "-z", px, px, str(MASTER), "--out", str(out)], check=True, capture_output=True)
        ICNS.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(ICNS)], check=True)


def main() -> None:
    write_sizes(draw_icon())
    write_icns()
    print(f"Icons written to {MASTER.parent} and {STATIC}")


if __name__ == "__main__":
    main()
