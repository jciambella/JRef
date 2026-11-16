"""Render the JRef icon assets from the vector logo ("ribbon J", drawn by ``bibmanager.gui.icons``).

Outputs:
* ``bibmanager/gui/assets/jref_glyph.png``  256 px  the logo tile (About box, Welcome dialog);
* ``bibmanager/gui/assets/jref_tile.png``   512 px  the tile with the mark, "JRef" and the subtitle;
* ``packaging/branding/jref_icon.png``     1024 px  the logo, for reuse elsewhere (Android icon, web).

The master artwork is ``packaging/branding/jref_logo_ribbon.svg``; ``icons.paint_app_icon`` draws the same shapes,
so the window/taskbar/.ico icon (``icons.app_icon``) needs no image file.

    python packaging/branding/make_icons.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
ASSETS = REPO / "bibmanager" / "gui" / "assets"


def main() -> int:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    sys.path.insert(0, str(REPO))
    from PySide6.QtCore import QRectF, Qt
    from PySide6.QtGui import QColor, QFont, QImage, QPainter
    from PySide6.QtWidgets import QApplication

    from bibmanager import APP_NAME, APP_SUBTITLE
    from bibmanager.gui import icons, theme

    app = QApplication.instance() or QApplication([])
    theme.load_brand_fonts()
    ASSETS.mkdir(parents=True, exist_ok=True)

    icons.app_icon_image(256).save(str(ASSETS / "jref_glyph.png"))
    icons.app_icon_image(1024).save(str(HERE / "jref_icon.png"))

    size = 512
    image = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    p = QPainter(image)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
    icons.paint_tile(p, QRectF(2, 2, size - 4, size - 4), 0.22 * size)
    # the mark spans x 32..224, y 50..219 of its 256-unit grid: scale it into the upper part of the tile
    scale = 1.42
    p.save()
    p.translate(size / 2 - 128 * scale, 40 - 50 * scale + 6)
    p.scale(scale, scale)
    icons.paint_logo_mark(p, size)
    p.restore()
    serif = QFont(theme.serif_family(), 1)
    serif.setPixelSize(104)
    serif.setWeight(QFont.Weight.DemiBold)
    p.setFont(serif)
    p.setPen(QColor("#eef2f8"))
    p.drawText(QRectF(0, 318, size, 120), int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop), APP_NAME)
    sans = QFont(theme.ui_font_family() or "Sans Serif", 1)
    sans.setPixelSize(30)
    p.setFont(sans)
    p.setPen(QColor("#9fb0c8"))
    p.drawText(QRectF(0, 440, size, 40), int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop), APP_SUBTITLE)
    p.end()
    image.save(str(ASSETS / "jref_tile.png"))
    for path in (ASSETS / "jref_glyph.png", ASSETS / "jref_tile.png", HERE / "jref_icon.png"):
        print(path.relative_to(REPO))
    del app
    return 0


if __name__ == "__main__":
    sys.exit(main())
