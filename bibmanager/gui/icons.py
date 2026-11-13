"""Monoline icons in the spirit of Fluent System Icons, drawn with QPainter at run time, plus the app logo.

UI icons use no image assets and no third-party artwork. The application icon is the JRef logo
supplied by the author; its simplified mark (``paint_app_icon``) is the window/taskbar/.ico icon, and the full
artwork (``assets/jref_*.png``, from ``packaging/branding/make_icons.py``) appears in About and Welcome.
Each UI icon is drawn on a 24-unit grid and rendered at
16/20/24/32 px (2x device-pixel ratio), so strokes stay crisp at every size. Colours follow the
active theme: ``set_theme`` clears the caches and the window re-applies its icons.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from functools import cache
from pathlib import Path
from typing import Any, TypeVar

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QGuiApplication,
    QIcon,
    QImage,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QPolygonF,
)

F = TypeVar("F", bound=Callable[..., QIcon])

_SIZES = (16, 20, 24, 32)
_DPR = 2.0
_ink = QColor("#404040")
_accent = QColor("#005fb8")
_on_accent = QColor("#ffffff")
_star = QColor("#e8a317")
_warning = QColor("#9d5d00")
_danger = QColor("#c42b1c")
_muted = QColor("#8b8b8b")
_registry: list[Any] = []


def themed(fn: F) -> F:
    cached = cache(fn)
    _registry.append(cached)
    return cached  # type: ignore[return-value]


def set_theme(theme: Any) -> None:
    """Adopt the colours of a ``theme.Theme`` and invalidate every cached icon."""
    global _ink, _accent, _on_accent, _star, _warning, _danger, _muted
    from .theme import qcolor

    _ink, _accent, _on_accent = qcolor(theme.icon), qcolor(theme.accent), qcolor(theme.accent_text)
    _star, _warning, _danger = qcolor(theme.star), qcolor(theme.warning), qcolor(theme.danger)
    _muted = qcolor(theme.text_tertiary)
    for fn in _registry:
        fn.cache_clear()


def _icon(draw: Callable[[QPainter], None]) -> QIcon:
    icon = QIcon()
    for size in _SIZES:
        pixmap = QPixmap(int(size * _DPR), int(size * _DPR))
        pixmap.setDevicePixelRatio(_DPR)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        painter.scale(size / 24.0, size / 24.0)
        draw(painter)
        painter.end()
        icon.addPixmap(pixmap)
    return icon


def _pen(color: QColor, width: float = 1.5) -> QPen:
    pen = QPen(color, width)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    return pen


def _stroke(p: QPainter, color: QColor | None = None, width: float = 1.5) -> None:
    p.setPen(_pen(color or _ink, width))
    p.setBrush(Qt.BrushStyle.NoBrush)


def _clear(p: QPainter, draw: Callable[[], None]) -> None:
    """Punch a transparent hole (used to separate overlapping outlines and badges)."""
    p.save()
    p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Clear)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(0, 0, 0))
    draw()
    p.restore()


def _document_path(x: float, y: float, w: float, h: float, fold: float = 4.5) -> QPainterPath:
    path = QPainterPath()
    path.moveTo(x + 1.5, y)
    path.lineTo(x + w - fold, y)
    path.lineTo(x + w, y + fold)
    path.lineTo(x + w, y + h - 1.5)
    path.quadTo(x + w, y + h, x + w - 1.5, y + h)
    path.lineTo(x + 1.5, y + h)
    path.quadTo(x, y + h, x, y + h - 1.5)
    path.lineTo(x, y + 1.5)
    path.quadTo(x, y, x + 1.5, y)
    return path


def _document(p: QPainter, x: float = 5.5, y: float = 3, w: float = 13, h: float = 18) -> None:
    _stroke(p)
    p.drawPath(_document_path(x, y, w, h))
    p.drawPolyline(QPolygonF([QPointF(x + w - 4.5, y), QPointF(x + w - 4.5, y + 4.5), QPointF(x + w, y + 4.5)]))


def _folder_path(x: float = 2.5, y: float = 5, w: float = 19, h: float = 14) -> QPainterPath:
    path = QPainterPath()
    path.moveTo(x, y + 1.5)
    path.quadTo(x, y, x + 1.5, y)
    path.lineTo(x + 6, y)
    path.lineTo(x + 8, y + 2.2)
    path.lineTo(x + w - 1.5, y + 2.2)
    path.quadTo(x + w, y + 2.2, x + w, y + 3.7)
    path.lineTo(x + w, y + h - 1.5)
    path.quadTo(x + w, y + h, x + w - 1.5, y + h)
    path.lineTo(x + 1.5, y + h)
    path.quadTo(x, y + h, x, y + h - 1.5)
    path.closeSubpath()
    return path


def _badge(p: QPainter, cx: float, cy: float, *, plus: bool) -> None:
    _clear(p, lambda: p.drawEllipse(QPointF(cx, cy), 5.4, 5.4))
    _stroke(p, _ink, 1.6)
    p.drawLine(QPointF(cx - 3, cy), QPointF(cx + 3, cy))
    if plus:
        p.drawLine(QPointF(cx, cy - 3), QPointF(cx, cy + 3))


def _star_polygon(cx: float, cy: float, outer: float, inner: float) -> QPolygonF:
    points = []
    for i in range(10):
        radius = outer if i % 2 == 0 else inner
        angle = -math.pi / 2 + i * math.pi / 5
        points.append(QPointF(cx + radius * math.cos(angle), cy + radius * math.sin(angle)))
    return QPolygonF(points)


# --------------------------------------------------------------------------- list glyphs
@themed
def star(filled: bool) -> QIcon:
    def draw(p: QPainter) -> None:
        poly = _star_polygon(12, 12.6, 8.6, 3.9)
        if filled:
            p.setPen(_pen(_star, 1.2))
            p.setBrush(_star)
        else:
            color = QColor(_muted)
            color.setAlphaF(0.75)
            p.setPen(_pen(color, 1.3))
            p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPolygon(poly)

    return _icon(draw)


@themed
def read_dot(unread: bool) -> QIcon:
    def draw(p: QPainter) -> None:
        if unread:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(_accent)
            p.drawEllipse(QPointF(12, 12), 4.0, 4.0)

    return _icon(draw)


@themed
def unread_header() -> QIcon:
    def draw(p: QPainter) -> None:
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(_muted)
        p.drawEllipse(QPointF(12, 12), 3.2, 3.2)

    return _icon(draw)


@themed
def pdf_file() -> QIcon:
    def draw(p: QPainter) -> None:
        _document(p, 6.5, 2.5, 13, 19)
        _clear(p, lambda: p.drawRoundedRect(QRectF(2.5, 11, 14, 9), 2, 2))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(_danger)
        p.drawRoundedRect(QRectF(3.5, 12, 12, 7), 1.6, 1.6)
        font = QFont()
        font.setPixelSize(5)
        font.setBold(True)
        p.setFont(font)
        p.setPen(QColor("white"))
        p.drawText(QRectF(3.5, 12, 12, 7), Qt.AlignmentFlag.AlignCenter, "PDF")

    return _icon(draw)


@themed
def document() -> QIcon:
    return _icon(_document)


def _paperclip(p: QPainter, color: QColor | None = None, width: float = 1.5) -> None:
    """A paper clip, tilted 40 degrees (supplementary files)."""
    p.save()
    p.translate(12, 12)
    p.rotate(40)
    p.translate(-12, -12)
    _stroke(p, color, width)
    path = QPainterPath()
    path.moveTo(14.6, 7.0)
    path.lineTo(14.6, 16.0)
    path.arcTo(QRectF(8.4, 12.9, 6.2, 6.2), 0, -180)  # bottom bend, right -> left
    path.lineTo(8.4, 5.6)
    path.arcTo(QRectF(8.4, 1.6, 4.4, 4.4), 180, -180)  # top bend, left -> right
    path.lineTo(12.8, 15.4)
    path.arcTo(QRectF(10.4, 14.2, 2.4, 2.4), 0, -180)
    path.lineTo(10.4, 7.6)
    p.drawPath(path)
    p.restore()


@themed
def paperclip() -> QIcon:
    return _icon(_paperclip)


@themed
def pdf_with_supplements() -> QIcon:
    """PDF glyph with a small paper clip: the document also has supplementary files."""

    def draw(p: QPainter) -> None:
        _document(p, 6.5, 2.5, 13, 19)
        _clear(p, lambda: p.drawRoundedRect(QRectF(2.5, 11, 14, 9), 2, 2))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(_danger)
        p.drawRoundedRect(QRectF(3.5, 12, 12, 7), 1.6, 1.6)
        font = QFont()
        font.setPixelSize(5)
        font.setBold(True)
        p.setFont(font)
        p.setPen(QColor("white"))
        p.drawText(QRectF(3.5, 12, 12, 7), Qt.AlignmentFlag.AlignCenter, "PDF")
        _clear(p, lambda: p.drawEllipse(QPointF(19, 5), 5.2, 5.2))
        p.save()
        p.translate(13.2, -0.6)
        p.scale(0.5, 0.5)
        _paperclip(p, _accent, 2.6)
        p.restore()

    return _icon(draw)


# --------------------------------------------------------------------------- navigation
def _two_documents(p: QPainter, color: QColor | None = None, width: float = 1.5) -> None:
    _stroke(p, color, width)
    p.drawPath(_document_path(8, 2.5, 12, 16, 3.5))
    _clear(p, lambda: p.drawRoundedRect(QRectF(3, 5.5, 14, 17), 2, 2))
    _stroke(p, color, width)
    p.drawPath(_document_path(4, 6.5, 12, 15, 3.5))


@themed
def all_documents() -> QIcon:
    return _icon(_two_documents)


@themed
def recently_added() -> QIcon:
    def draw(p: QPainter) -> None:
        _stroke(p)
        p.drawEllipse(QPointF(12, 12), 8.5, 8.5)
        p.drawPolyline(QPolygonF([QPointF(12, 7), QPointF(12, 12), QPointF(15.5, 14)]))

    return _icon(draw)


@themed
def recently_read() -> QIcon:
    def draw(p: QPainter) -> None:
        _stroke(p)
        left = QPainterPath()
        left.moveTo(12, 7)
        left.cubicTo(9.5, 5.2, 6, 4.8, 2.8, 5.6)
        left.lineTo(2.8, 18.4)
        left.cubicTo(6, 17.6, 9.5, 18, 12, 19.6)
        right = QPainterPath()
        right.moveTo(12, 7)
        right.cubicTo(14.5, 5.2, 18, 4.8, 21.2, 5.6)
        right.lineTo(21.2, 18.4)
        right.cubicTo(18, 17.6, 14.5, 18, 12, 19.6)
        p.drawPath(left)
        p.drawPath(right)
        p.drawLine(QPointF(12, 7), QPointF(12, 19.6))

    return _icon(draw)


@themed
def favorites() -> QIcon:
    def draw(p: QPainter) -> None:
        _stroke(p)
        p.drawPolygon(_star_polygon(12, 12.6, 9, 4.1))

    return _icon(draw)


@themed
def needs_review() -> QIcon:
    def draw(p: QPainter) -> None:
        _stroke(p, _warning, 1.6)
        p.drawEllipse(QPointF(12, 12), 8.5, 8.5)
        p.drawLine(QPointF(12, 7.6), QPointF(12, 13))
        p.setPen(_pen(_warning, 2.2))
        p.drawPoint(QPointF(12, 16.3))

    return _icon(draw)


@themed
def person() -> QIcon:
    def draw(p: QPainter) -> None:
        _stroke(p)
        p.drawEllipse(QPointF(12, 8), 4, 4)
        body = QPainterPath()
        body.moveTo(4.5, 20.5)
        body.cubicTo(4.8, 15.5, 8, 13.8, 12, 13.8)
        body.cubicTo(16, 13.8, 19.2, 15.5, 19.5, 20.5)
        p.drawPath(body)

    return _icon(draw)


@themed
def unsorted() -> QIcon:
    def draw(p: QPainter) -> None:
        _stroke(p)
        tray = QPainterPath()
        tray.moveTo(3, 13)
        tray.lineTo(6, 5.5)
        tray.lineTo(18, 5.5)
        tray.lineTo(21, 13)
        tray.lineTo(21, 18)
        tray.quadTo(21, 19.5, 19.5, 19.5)
        tray.lineTo(4.5, 19.5)
        tray.quadTo(3, 19.5, 3, 18)
        tray.closeSubpath()
        p.drawPath(tray)
        p.drawPolyline(
            QPolygonF(
                [
                    QPointF(3, 13),
                    QPointF(8, 13),
                    QPointF(9.5, 15.5),
                    QPointF(14.5, 15.5),
                    QPointF(16, 13),
                    QPointF(21, 13),
                ]
            )  # fmt: skip
        )

    return _icon(draw)


@themed
def folder() -> QIcon:
    def draw(p: QPainter) -> None:
        _stroke(p)
        p.drawPath(_folder_path())

    return _icon(draw)


@themed
def folder_add() -> QIcon:
    def draw(p: QPainter) -> None:
        _stroke(p)
        p.drawPath(_folder_path(2, 4.5, 18, 13.5))
        _badge(p, 18, 17.5, plus=True)

    return _icon(draw)


@themed
def folder_remove() -> QIcon:
    def draw(p: QPainter) -> None:
        _stroke(p)
        p.drawPath(_folder_path(2, 4.5, 18, 13.5))
        _badge(p, 18, 17.5, plus=False)

    return _icon(draw)


@themed
def add_small() -> QIcon:
    def draw(p: QPainter) -> None:
        _stroke(p, _muted, 1.6)
        p.drawLine(QPointF(12, 6), QPointF(12, 18))
        p.drawLine(QPointF(6, 12), QPointF(18, 12))

    return _icon(draw)


# --------------------------------------------------------------------------- commands
@themed
def add(on_accent: bool = False) -> QIcon:
    def draw(p: QPainter) -> None:
        _stroke(p, _on_accent if on_accent else _ink, 1.7)
        p.drawLine(QPointF(12, 5), QPointF(12, 19))
        p.drawLine(QPointF(5, 12), QPointF(19, 12))

    return _icon(draw)


@themed
def sync() -> QIcon:
    def draw(p: QPainter) -> None:
        _stroke(p, _ink, 1.6)
        rect = QRectF(4.5, 4.5, 15, 15)
        p.drawArc(rect, 20 * 16, 150 * 16)
        p.drawArc(rect, 200 * 16, 150 * 16)
        p.drawPolyline(QPolygonF([QPointF(3.6, 6.8), QPointF(5.1, 10.6), QPointF(8.9, 9.4)]))
        p.drawPolyline(QPolygonF([QPointF(20.4, 17.2), QPointF(18.9, 13.4), QPointF(15.1, 14.6)]))

    return _icon(draw)


@themed
def help_circle() -> QIcon:
    def draw(p: QPainter) -> None:
        _stroke(p)
        p.drawEllipse(QPointF(12, 12), 8.5, 8.5)
        q = QPainterPath()
        q.moveTo(9.4, 9.6)
        q.cubicTo(9.4, 7.8, 10.6, 6.9, 12.1, 6.9)
        q.cubicTo(13.7, 6.9, 14.8, 7.9, 14.8, 9.4)
        q.cubicTo(14.8, 11.4, 12.1, 11.6, 12.1, 13.6)
        p.drawPath(q)
        p.setPen(_pen(_ink, 2.1))
        p.drawPoint(QPointF(12.1, 16.6))

    return _icon(draw)


@themed
def settings() -> QIcon:
    def draw(p: QPainter) -> None:
        _stroke(p)
        teeth = 8
        points = []
        for i in range(teeth * 4):
            angle = 2 * math.pi * (i - 0.5) / (teeth * 4)
            radius = 9.0 if (i % 4) in (1, 2) else 7.0
            points.append(QPointF(12 + radius * math.cos(angle), 12 + radius * math.sin(angle)))
        p.drawPolygon(QPolygonF(points))
        p.drawEllipse(QPointF(12, 12), 3, 3)

    return _icon(draw)


@themed
def search() -> QIcon:
    def draw(p: QPainter) -> None:
        _stroke(p, _ink, 1.6)
        p.drawEllipse(QPointF(10, 10), 6, 6)
        p.drawLine(QPointF(14.4, 14.4), QPointF(20, 20))

    return _icon(draw)


@themed
def funnel() -> QIcon:
    def draw(p: QPainter) -> None:
        _stroke(p)
        p.drawPolygon(
            QPolygonF(
                [QPointF(4, 5), QPointF(20, 5), QPointF(14, 12.5), QPointF(14, 19), QPointF(10, 17), QPointF(10, 12.5)]
            )  # fmt: skip
        )

    return _icon(draw)


@themed
def open_link() -> QIcon:
    def draw(p: QPainter) -> None:
        _stroke(p)
        path = QPainterPath()
        path.moveTo(10.5, 5)
        path.lineTo(6.5, 5)
        path.quadTo(5, 5, 5, 6.5)
        path.lineTo(5, 17.5)
        path.quadTo(5, 19, 6.5, 19)
        path.lineTo(17.5, 19)
        path.quadTo(19, 19, 19, 17.5)
        path.lineTo(19, 13.5)
        p.drawPath(path)
        p.drawLine(QPointF(12, 12), QPointF(19.5, 4.5))
        p.drawPolyline(QPolygonF([QPointF(14, 4.5), QPointF(19.5, 4.5), QPointF(19.5, 10)]))

    return _icon(draw)


@themed
def copy() -> QIcon:
    def draw(p: QPainter) -> None:
        _stroke(p)
        p.drawRoundedRect(QRectF(8, 7, 11, 13.5), 2, 2)
        path = QPainterPath()
        path.moveTo(16, 4.5)
        path.lineTo(7, 4.5)
        path.quadTo(5, 4.5, 5, 6.5)
        path.lineTo(5, 16.5)
        p.drawPath(path)

    return _icon(draw)


@themed
def delete() -> QIcon:
    def draw(p: QPainter) -> None:
        _stroke(p)
        p.drawLine(QPointF(4.5, 6.5), QPointF(19.5, 6.5))
        p.drawPolyline(QPolygonF([QPointF(9.5, 6.5), QPointF(9.5, 4), QPointF(14.5, 4), QPointF(14.5, 6.5)]))
        path = QPainterPath()
        path.moveTo(6.5, 6.5)
        path.lineTo(7.4, 18.6)
        path.quadTo(7.6, 20, 9, 20)
        path.lineTo(15, 20)
        path.quadTo(16.4, 20, 16.6, 18.6)
        path.lineTo(17.5, 6.5)
        p.drawPath(path)

    return _icon(draw)


@themed
def chevron_down() -> QIcon:
    def draw(p: QPainter) -> None:
        _stroke(p, _ink, 1.6)
        p.drawPolyline(QPolygonF([QPointF(7, 9.5), QPointF(12, 14.5), QPointF(17, 9.5)]))

    return _icon(draw)


@themed
def empty_library() -> QIcon:
    """Empty-state illustration, drawn in the tertiary text colour."""

    def draw(p: QPainter) -> None:
        _two_documents(p, _muted, 1.0)
        _stroke(p, _muted, 1.0)
        for y, x2 in ((12, 13), (15, 13), (18, 11)):
            p.drawLine(QPointF(6.5, y), QPointF(x2, y))

    return _icon(draw)


ASSETS_DIR = Path(__file__).resolve().parent / "assets"
APP_ICON_SIZES = (16, 20, 24, 32, 40, 48, 64, 96, 128, 256)
# Palette of the JRef logo (navy tile, teal "J", gold book).
_TILE_TOP, _TILE_BOTTOM = QColor("#2a3b55"), QColor("#121a28")
_J_TOP, _J_BOTTOM = QColor("#3fd3e3"), QColor("#1b9fbc")
_BOOK, _BOOK_BAND = QColor("#e9ad43"), QColor("#9a6418")


def paint_app_icon(p: QPainter, size: int) -> None:
    """The simplified JRef mark -- a teal "J" holding a gold book on a navy tile -- drawn as vectors
    on a 32-unit grid so it stays crisp from 16 px (title bar, taskbar) to 256 px (Explorer). Small sizes
    drop the hairline details (tile border, book bands) and thicken the J."""
    p.save()
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.scale(size / 32.0, size / 32.0)
    tile = QRectF(1, 1, 30, 30)
    gradient = QLinearGradient(0, 1, 0, 31)
    gradient.setColorAt(0, _TILE_TOP)
    gradient.setColorAt(1, _TILE_BOTTOM)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QBrush(gradient))
    p.drawRoundedRect(tile, 7, 7)
    if size >= 32:
        hair = 32 / size
        p.setPen(QPen(QColor(255, 255, 255, 34), hair))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(tile.adjusted(hair / 2, hair / 2, -hair / 2, -hair / 2), 6.6, 6.6)
    # the book, leaning against the stem of the J
    p.save()
    p.translate(18.6, 20.4)
    p.rotate(-14)
    p.translate(-18.6, -20.4)
    book = QRectF(13.0, 10.6, 5.0, 9.8)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(_BOOK)
    p.drawRoundedRect(book, 0.8, 0.8)
    if size >= 24:
        p.setBrush(_BOOK_BAND)
        p.drawRect(QRectF(book.left(), book.top() + 1.5, book.width(), 0.7))
        p.drawRect(QRectF(book.left(), book.bottom() - 2.2, book.width(), 0.7))
    p.restore()
    # the J: top bar, stem, hook
    j = QPainterPath()
    j.moveTo(13.0, 8.2)
    j.lineTo(25.0, 8.2)
    j.moveTo(21.2, 8.2)
    j.lineTo(21.2, 18.6)
    j.arcTo(QRectF(10.0, 13.0, 11.2, 11.2), 0, -180)
    j.lineTo(10.0, 17.0)
    j_gradient = QLinearGradient(0, 6, 0, 26)
    j_gradient.setColorAt(0, _J_TOP)
    j_gradient.setColorAt(1, _J_BOTTOM)
    pen = QPen(QBrush(j_gradient), 4.0 if size >= 24 else 4.4)
    pen.setCapStyle(Qt.PenCapStyle.FlatCap)
    pen.setJoinStyle(Qt.PenJoinStyle.MiterJoin)
    p.setPen(pen)
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawPath(j)
    p.restore()


def app_icon_image(size: int) -> QImage:
    image = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    paint_app_icon(painter, size)
    painter.end()
    return image


@cache
def app_icon() -> QIcon:
    """Application icon (window, taskbar, .ico): the simplified mark at every standard size."""
    icon = QIcon()
    for size in APP_ICON_SIZES:
        icon.addPixmap(QPixmap.fromImage(app_icon_image(size)))
    return icon


def brand_pixmap(size: int, *, with_name: bool = True) -> QPixmap:
    """Full logo artwork for dialogs (About, Welcome): the tile with the name, or the emblem alone."""
    source = QPixmap(str(ASSETS_DIR / ("jref_tile.png" if with_name else "jref_glyph.png")))
    ratio = QGuiApplication.primaryScreen().devicePixelRatio() if QGuiApplication.primaryScreen() else 1.0
    if source.isNull():  # pragma: no cover - broken installation: fall back to the simplified mark
        source = QPixmap.fromImage(app_icon_image(round(size * ratio)))
    pix = source.scaled(
        round(size * ratio),
        round(size * ratio),
        Qt.AspectRatioMode.KeepAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    )
    pix.setDevicePixelRatio(ratio)
    return pix
