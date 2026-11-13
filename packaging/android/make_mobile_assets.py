"""Render the image assets of the Android app (``bibmanager/mobile/assets``). Needs PySide6 (desktop only).

* UI icons: the monoline icons of the desktop application (``bibmanager.gui.icons``) plus a few drawn here,
  exported as white 96 px PNGs with transparency; the app tints them at run time.
* ``icon.png`` (512 px) and ``presplash.png``: the JRef mark and the lettered tile.

Run after changing an icon:  python packaging/android/make_mobile_assets.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from PySide6.QtCore import QPointF, QRectF, Qt  # noqa: E402
from PySide6.QtGui import QColor, QImage, QPainter, QPainterPath, QPen  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from bibmanager.gui import icons  # noqa: E402

OUT = REPO / "bibmanager" / "mobile" / "assets"
SIZE = 96
WHITE = QColor(255, 255, 255)


def whiten(image: QImage) -> QImage:
    """Keep the shape (alpha), drop the colours: the app multiplies the texture by the colour it wants."""
    image = image.convertToFormat(QImage.Format.Format_ARGB32)
    for y in range(image.height()):
        for x in range(image.width()):
            alpha = QColor.fromRgba(image.pixel(x, y)).alpha()
            image.setPixelColor(x, y, QColor(255, 255, 255, alpha))
    return image


def from_desktop(icon) -> QImage:  # type: ignore[no-untyped-def]
    return whiten(icon.pixmap(SIZE, SIZE).toImage().scaled(SIZE, SIZE))


def drawn(paint) -> QImage:  # type: ignore[no-untyped-def]
    """A 24 x 24 monoline drawing (1.7 px round strokes), like the desktop icons."""
    image = QImage(SIZE, SIZE, QImage.Format.Format_ARGB32)
    image.fill(Qt.GlobalColor.transparent)
    p = QPainter(image)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.scale(SIZE / 24, SIZE / 24)
    pen = QPen(WHITE, 1.8)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    p.setPen(pen)
    p.setBrush(Qt.BrushStyle.NoBrush)
    paint(p)
    p.end()
    return image


def lines(*segments: tuple[float, float, float, float]):  # type: ignore[no-untyped-def]
    def paint(p: QPainter) -> None:
        for x1, y1, x2, y2 in segments:
            p.drawLine(QPointF(x1, y1), QPointF(x2, y2))

    return paint


def _edit(p: QPainter) -> None:
    path = QPainterPath()
    path.moveTo(5, 19)
    path.lineTo(5.8, 15.4)
    path.lineTo(15.6, 5.6)
    path.lineTo(18.4, 8.4)
    path.lineTo(8.6, 18.2)
    path.closeSubpath()
    p.drawPath(path)
    p.drawLine(QPointF(13.6, 7.6), QPointF(16.4, 10.4))


def _share(p: QPainter) -> None:
    for cx, cy in ((17.5, 5.5), (6.5, 12), (17.5, 18.5)):
        p.drawEllipse(QPointF(cx, cy), 2.3, 2.3)
    p.drawLine(QPointF(8.6, 10.8), QPointF(15.4, 6.7))
    p.drawLine(QPointF(8.6, 13.2), QPointF(15.4, 17.3))


def _more(p: QPainter) -> None:
    p.setBrush(WHITE)
    for cy in (5.5, 12, 18.5):
        p.drawEllipse(QPointF(12, cy), 1.1, 1.1)


def _phone(p: QPainter) -> None:
    p.drawRoundedRect(QRectF(7, 3, 10, 18), 2, 2)
    p.drawLine(QPointF(11, 17.8), QPointF(13, 17.8))


def _sort(p: QPainter) -> None:
    for y, x2 in ((7, 19), (12, 15), (17, 11)):
        p.drawLine(QPointF(5, y), QPointF(x2, y))


def _tag(p: QPainter) -> None:
    path = QPainterPath()
    path.moveTo(4, 5)
    path.lineTo(11.5, 5)
    path.lineTo(20, 13.5)
    path.lineTo(13.5, 20)
    path.lineTo(4, 11.5)
    path.closeSubpath()
    p.drawPath(path)
    p.drawEllipse(QPointF(8, 9), 1.1, 1.1)


def _note(p: QPainter) -> None:
    p.drawRoundedRect(QRectF(5, 4, 14, 16), 2, 2)
    for y in (9, 12.5, 16):
        p.drawLine(QPointF(8, y), QPointF(16 if y < 16 else 13, y))


DRAWN = {
    "menu": lines((4, 7, 20, 7), (4, 12, 20, 12), (4, 17, 20, 17)),
    "back": lines((19, 12, 5.5, 12), (11, 6, 5, 12), (11, 18, 5, 12)),
    "close": lines((6, 6, 18, 18), (18, 6, 6, 18)),
    "check": lines((5, 12.5, 10, 17.5), (10, 17.5, 19, 7)),
    "chevron_right": lines((9.5, 6, 15.5, 12), (15.5, 12, 9.5, 18)),
    "up": lines((12, 19, 12, 5.5), (6, 11, 12, 5), (18, 11, 12, 5)),
    "plus": lines((12, 5, 12, 19), (5, 12, 19, 12)),
    "edit": _edit,
    "share": _share,
    "more": _more,
    "phone": _phone,
    "sort": _sort,
    "tag": _tag,
    "note": _note,
}
DESKTOP = {
    "star": lambda: icons.star(True),
    "star_outline": lambda: icons.star(False),
    "pdf": icons.pdf_file,
    "document": icons.document,
    "all_documents": icons.all_documents,
    "recently_added": icons.recently_added,
    "recently_read": icons.recently_read,
    "favorites": icons.favorites,
    "needs_review": icons.needs_review,
    "person": icons.person,
    "unsorted": icons.unsorted,
    "folder": icons.folder,
    "folder_add": icons.folder_add,
    "sync": icons.sync,
    "help": icons.help_circle,
    "settings": icons.settings,
    "search": icons.search,
    "open_link": icons.open_link,
    "copy": icons.copy,
    "delete": icons.delete,
}


def main() -> int:
    app = QApplication.instance() or QApplication([])
    OUT.mkdir(parents=True, exist_ok=True)
    icons._SIZES, icons._DPR = (SIZE,), 1.0  # rasterise the desktop icons directly at the export size
    for cached in icons._registry:
        cached.cache_clear()
    for name, factory in DESKTOP.items():
        from_desktop(factory()).save(str(OUT / f"{name}.png"))
    for name, paint in DRAWN.items():
        drawn(paint).save(str(OUT / f"{name}.png"))
    icons.app_icon_image(512).save(str(OUT / "icon.png"))
    tile = QImage(str(REPO / "bibmanager" / "gui" / "assets" / "jref_tile.png"))
    splash = QImage(1024, 1024, QImage.Format.Format_ARGB32)
    splash.fill(QColor("#141C2E"))
    p = QPainter(splash)
    p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    p.drawImage(QRectF(256, 256, 512, 512), tile)
    p.end()
    splash.save(str(OUT / "presplash.png"))
    tile.scaled(256, 256, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation).save(
        str(OUT / "logo.png")
    )
    print(f"{len(DESKTOP) + len(DRAWN)} icons, icon.png, presplash.png, logo.png -> {OUT.relative_to(REPO)}")
    del app
    return 0


if __name__ == "__main__":
    sys.exit(main())
