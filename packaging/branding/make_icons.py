"""Derive the JRef icon assets from the logo artwork (``JBibManager-logo-source.jpg``, the author's own).

Outputs (``bibmanager/gui/assets/``), used by the About box, the first-run Welcome and the installer pages:
* ``jref_tile.png``  512 px  rounded tile; the lettering of the artwork is replaced by "JRef" and the subtitle;
* ``jref_glyph.png`` 256 px  emblem only, re-framed on the tile background.

The window/taskbar/.ico icon is not derived from these: it is the simplified vector mark drawn by
``bibmanager.gui.icons.paint_app_icon``; this script also exports that mark as
``packaging/branding/jref_icon.png`` (1024 px) and ``jref_icon.svg`` for reuse elsewhere (Android icon).
"""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

HERE = Path(__file__).resolve().parent
SOURCE = HERE / "JBibManager-logo-source.jpg"
ASSETS = HERE.parents[1] / "bibmanager" / "gui" / "assets"
APP_NAME, APP_SUBTITLE = "JRef", "Journal Reference Manager"
# Geometry measured on the 2752 x 1536 source: the tile (incl. its light border) spans x 848..1904, y 241..1297.
TILE_BOX = (848, 241, 1904, 1297)
TILE_RADIUS = 0.145  # corner radius / side
GLYPH_BOX = (180, 70, 900, 790)  # inside the tile crop: emblem without the lettering


def rounded_mask(size: int, radius_fraction: float, inset: float = 0.0) -> Image.Image:
    """Anti-aliased rounded-square alpha mask (drawn at 4x and downsampled)."""
    scale = 4
    big = Image.new("L", (size * scale, size * scale), 0)
    r = radius_fraction * size * scale
    i = inset * scale
    ImageDraw.Draw(big).rounded_rectangle((i, i, size * scale - 1 - i, size * scale - 1 - i), radius=r, fill=255)
    return big.resize((size, size), Image.Resampling.LANCZOS)


def framed(image: Image.Image, size: int, *, border: bool = True) -> Image.Image:
    """Square crop -> rounded icon with a thin light border, like the original tile."""
    out = image.convert("RGB").resize((size, size), Image.Resampling.LANCZOS).convert("RGBA")
    if border:
        scale = 4
        ring = Image.new("RGBA", (size * scale, size * scale), (0, 0, 0, 0))
        width = max(scale, round(size * scale * 0.012))
        ImageDraw.Draw(ring).rounded_rectangle(
            (width / 2, width / 2, size * scale - 1 - width / 2, size * scale - 1 - width / 2),
            radius=TILE_RADIUS * size * scale,
            outline=(120, 140, 172, 200),
            width=width,
        )
        out.alpha_composite(ring.resize((size, size), Image.Resampling.LANCZOS))
    out.putalpha(rounded_mask(size, TILE_RADIUS))
    return out


WORDMARK_ROWS = (776, 968)  # rows of the tile crop that hold the lettering of the source artwork
FONT_DIRS = (Path("/usr/share/fonts/opentype/inter"), Path("C:/Windows/Fonts"), Path("/usr/share/fonts/truetype/dejavu"))
FONT_BOLD = ("Inter-Bold.otf", "segoeuib.ttf", "DejaVuSans-Bold.ttf")
FONT_REGULAR = ("Inter-Medium.otf", "segoeui.ttf", "DejaVuSans.ttf")


def _font(names: tuple[str, ...], size: int) -> ImageFont.FreeTypeFont:
    for folder in FONT_DIRS:
        for name in names:
            if (folder / name).is_file():
                return ImageFont.truetype(str(folder / name), size)
    raise SystemExit(f"No suitable font found (looked for {names} in {[str(d) for d in FONT_DIRS]})")


def relettered(tile: Image.Image, name: str, subtitle: str) -> Image.Image:
    """The tile of the source artwork with its lettering replaced by ``name`` / ``subtitle``.

    The old lettering is painted over row by row with the tile's own background (sampled left and right of it,
    so the soft gradient is kept); the new one is typeset in the same two-tone style.
    """
    out = tile.convert("RGB").copy()
    width = out.width
    pixels = out.load()
    top, bottom = WORDMARK_ROWS
    for y in range(top, bottom):
        left = [pixels[x, y] for x in range(34, 54)]
        right = [pixels[x, y] for x in range(width - 54, width - 34)]
        a = tuple(sum(c[k] for c in left) / len(left) for k in range(3))
        b = tuple(sum(c[k] for c in right) / len(right) for k in range(3))
        for x in range(54, width - 54):
            t = (x - 54) / (width - 108)
            pixels[x, y] = tuple(round(a[k] + (b[k] - a[k]) * t) for k in range(3))
    draw = ImageDraw.Draw(out)
    bold = _font(FONT_BOLD, 132)
    first, rest = name[:1], name[1:]
    total = draw.textlength(name, font=bold)
    x = (width - total) / 2
    baseline = 884
    draw.text((x, baseline), first, font=bold, fill=(140, 176, 214), anchor="ls")
    draw.text((x + draw.textlength(first, font=bold), baseline), rest, font=bold, fill=(236, 241, 248), anchor="ls")
    small = _font(FONT_REGULAR, 43)
    draw.text((width / 2, 944), subtitle, font=small, fill=(150, 170, 198), anchor="ms")
    return out


def main() -> int:
    source = Image.open(SOURCE).convert("RGB")
    tile = source.crop(TILE_BOX)
    ASSETS.mkdir(parents=True, exist_ok=True)

    tile_png = relettered(tile, APP_NAME, APP_SUBTITLE).resize((512, 512), Image.Resampling.LANCZOS).convert("RGBA")
    tile_png.putalpha(rounded_mask(512, TILE_RADIUS, inset=0.5))
    tile_png.save(ASSETS / "jref_tile.png", optimize=True)

    glyph = framed(tile.crop(GLYPH_BOX), 256)
    glyph.save(ASSETS / "jref_glyph.png", optimize=True)

    for path in sorted(ASSETS.glob("jref_*.png")):
        print(path.relative_to(HERE.parents[1]), Image.open(path).size)
    export_mark()
    return 0


def export_mark() -> None:
    """The simplified vector mark as a 1024 px PNG and an SVG (skipped without PySide6)."""
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    sys.path.insert(0, str(HERE.parents[1]))
    try:
        from PySide6.QtCore import QRect, QSize
        from PySide6.QtGui import QPainter
        from PySide6.QtSvg import QSvgGenerator
        from PySide6.QtWidgets import QApplication

        from bibmanager.gui import icons
    except ImportError as exc:  # pragma: no cover
        print(f"Skipping the vector mark: {exc}")
        return
    app = QApplication.instance() or QApplication([])
    icons.app_icon_image(1024).save(str(HERE / "jref_icon.png"))
    svg = QSvgGenerator()
    svg.setFileName(str(HERE / "jref_icon.svg"))
    svg.setSize(QSize(256, 256))
    svg.setViewBox(QRect(0, 0, 256, 256))
    svg.setTitle(APP_NAME)
    painter = QPainter(svg)
    icons.paint_app_icon(painter, 256)
    painter.end()
    print("packaging/branding/jref_icon.png (1024, 1024), jref_icon.svg")
    del app


if __name__ == "__main__":
    sys.exit(main())
