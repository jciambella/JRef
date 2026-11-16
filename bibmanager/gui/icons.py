"""Monoline icons in the spirit of Fluent System Icons, drawn with QPainter at run time, plus the app logo.

UI icons use no image assets and no third-party artwork. The application icon is the JRef logo ("ribbon J",
``packaging/branding/jref_logo_ribbon.svg``) drawn as vectors by ``paint_app_icon`` (window, taskbar, .ico); the
tile with the lettering (``assets/jref_*.png``, from ``packaging/branding/make_icons.py``) appears in About and
Welcome.
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


# --------------------------------------------------------------------------- shelf and new views
_SHELF_INK = QColor("#e8ecf2")
_SHELF_MUTED = QColor("#9aa7ba")
_AMBER = QColor("#e9ad43")


@themed
def inbox(on_shelf: bool = True) -> QIcon:
    def draw(p: QPainter) -> None:
        _stroke(p, _SHELF_INK if on_shelf else _ink, 1.6)
        tray = QPainterPath()
        tray.moveTo(3.5, 13)
        tray.lineTo(8.5, 13)
        tray.lineTo(10, 16)
        tray.lineTo(14, 16)
        tray.lineTo(15.5, 13)
        tray.lineTo(20.5, 13)
        p.drawPath(tray)
        box = QPainterPath()
        box.moveTo(6, 5)
        box.lineTo(18, 5)
        box.lineTo(20.5, 13)
        box.lineTo(20.5, 18.5)
        box.quadTo(20.5, 19.5, 19.5, 19.5)
        box.lineTo(4.5, 19.5)
        box.quadTo(3.5, 19.5, 3.5, 18.5)
        box.lineTo(3.5, 13)
        box.closeSubpath()
        p.drawPath(box)

    return _icon(draw)


@themed
def library(on_shelf: bool = True) -> QIcon:
    def draw(p: QPainter) -> None:
        _stroke(p, _SHELF_INK if on_shelf else _ink, 1.6)
        p.drawRoundedRect(QRectF(4.5, 4.5, 4, 15), 0.8, 0.8)
        p.drawRoundedRect(QRectF(10.5, 4.5, 4, 15), 0.8, 0.8)
        p.save()
        p.translate(17.8, 19.2)
        p.rotate(-14)
        p.drawRoundedRect(QRectF(-1.8, -14.6, 3.6, 14.6), 0.8, 0.8)
        p.restore()

    return _icon(draw)


@themed
def project(on_shelf: bool = True) -> QIcon:
    """A project = one .bib file: a pair of braces in the brand amber."""

    def draw(p: QPainter) -> None:
        _stroke(p, _AMBER if on_shelf else _ink, 1.7)
        for mirror in (False, True):
            def x(v: float, m: bool = mirror) -> float:
                return 24 - v if m else v

            path = QPainterPath(QPointF(x(10.5), 4.5))
            path.cubicTo(x(8.6), 4.5, x(8.4), 5.6, x(8.4), 8)
            path.lineTo(x(8.4), 10)
            path.cubicTo(x(8.4), 11.4, x(7.8), 12, x(6.2), 12)
            path.cubicTo(x(7.8), 12, x(8.4), 12.6, x(8.4), 14)
            path.lineTo(x(8.4), 16)
            path.cubicTo(x(8.4), 18.4, x(8.6), 19.5, x(10.5), 19.5)
            p.drawPath(path)

    return _icon(draw)


@themed
def saved_search(on_shelf: bool = True) -> QIcon:
    def draw(p: QPainter) -> None:
        _stroke(p, _SHELF_MUTED if on_shelf else _muted, 1.6)
        p.drawEllipse(QPointF(10.5, 10.5), 5.5, 5.5)
        p.drawLine(QPointF(14.6, 14.6), QPointF(19.5, 19.5))

    return _icon(draw)


@themed
def cloud(on_shelf: bool = True) -> QIcon:
    def draw(p: QPainter) -> None:
        _stroke(p, _SHELF_INK if on_shelf else _ink, 1.6)
        path = QPainterPath(QPointF(7, 18))
        path.cubicTo(3.5, 18, 2.8, 12.6, 6.4, 11.6)
        path.cubicTo(6.8, 7.2, 12.6, 5.6, 15.3, 9.2)
        path.cubicTo(19.4, 8.6, 21.6, 13.4, 19.3, 16.2)
        path.cubicTo(18.6, 17.4, 17.8, 18, 17, 18)
        path.closeSubpath()
        p.drawPath(path)

    return _icon(draw)


@themed
def study(on_strong: bool = True) -> QIcon:
    """Speech bubble with two lines: Study and Ask AI."""

    def draw(p: QPainter) -> None:
        _stroke(p, QColor("#ffffff") if on_strong else _ink, 1.6)
        path = QPainterPath(QPointF(5, 5))
        path.lineTo(19, 5)
        path.quadTo(20, 5, 20, 6)
        path.lineTo(20, 15)
        path.quadTo(20, 16, 19, 16)
        path.lineTo(10, 16)
        path.lineTo(5.5, 19.5)
        path.lineTo(5.5, 16)
        path.quadTo(4, 16, 4, 15)
        path.lineTo(4, 6)
        path.quadTo(4, 5, 5, 5)
        p.drawPath(path)
        p.drawLine(QPointF(8, 9.2), QPointF(16, 9.2))
        p.drawLine(QPointF(8, 12.2), QPointF(13, 12.2))

    return _icon(draw)


@themed
def back(on_dark: bool = True) -> QIcon:
    def draw(p: QPainter) -> None:
        _stroke(p, _SHELF_INK if on_dark else _ink, 1.8)
        p.drawLine(QPointF(19, 12), QPointF(5, 12))
        p.drawPolyline(QPolygonF([QPointF(11, 6), QPointF(5, 12), QPointF(11, 18)]))

    return _icon(draw)


@themed
def close_small(on_dark: bool = True) -> QIcon:
    def draw(p: QPainter) -> None:
        _stroke(p, _SHELF_INK if on_dark else _ink, 2.0)
        p.drawLine(QPointF(7, 7), QPointF(17, 17))
        p.drawLine(QPointF(17, 7), QPointF(7, 17))

    return _icon(draw)


@themed
def check() -> QIcon:
    def draw(p: QPainter) -> None:
        _stroke(p, _ink, 2.0)
        p.drawPolyline(QPolygonF([QPointF(5, 12.5), QPointF(9.5, 17), QPointF(19, 7.5)]))

    return _icon(draw)


@themed
def note() -> QIcon:
    def draw(p: QPainter) -> None:
        _stroke(p, _ink, 1.5)
        path = QPainterPath(QPointF(5, 4))
        path.lineTo(19, 4)
        path.lineTo(19, 16)
        path.lineTo(15, 20)
        path.lineTo(5, 20)
        path.closeSubpath()
        p.drawPath(path)
        p.drawPolyline(QPolygonF([QPointF(15, 20), QPointF(15, 16), QPointF(19, 16)]))
        p.drawLine(QPointF(8, 9), QPointF(16, 9))
        p.drawLine(QPointF(8, 12.5), QPointF(13, 12.5))

    return _icon(draw)


ASSETS_DIR = Path(__file__).resolve().parent / "assets"
APP_ICON_SIZES = (16, 20, 24, 32, 40, 48, 64, 96, 128, 256)
# Palette of the JRef logo, "ribbon J": navy tile, teal journal cover, cream pages, amber bookmark ribbon.
_TILE_TOP, _TILE_BOTTOM = QColor("#2b3d58"), QColor("#111827")
_COVER_TOP, _COVER_BOTTOM = QColor("#4fd8e6"), QColor("#1695b3")
_RIBBON_TOP, _RIBBON_BOTTOM = QColor("#f4c463"), QColor("#c9862a")
_PAGE_TOP, _PAGE_BOTTOM = QColor("#fbf5e8"), QColor("#e8dcc2")
_PAGE_LINE, _GUTTER, _ENTRY_MARK = QColor("#c7b892"), QColor("#b9a57c"), QColor("#d9962f")


def _vertical(top: QColor, bottom: QColor, y0: float, y1: float) -> QBrush:
    gradient = QLinearGradient(0, y0, 0, y1)
    gradient.setColorAt(0, top)
    gradient.setColorAt(1, bottom)
    return QBrush(gradient)


def ribbon_path() -> QPainterPath:
    """The bookmark ribbon that falls out of the gutter and curls into the J (256-unit grid)."""
    path = QPainterPath()
    path.moveTo(117, 96)
    path.lineTo(141, 96)
    path.lineTo(141, 176)
    path.arcTo(QRectF(55, 133, 86, 86), 0, -180)  # outer bowl, (141,176) -> (55,176) through the bottom
    path.lineTo(55, 158)
    path.lineTo(67, 167)  # swallow-tail end of the ribbon
    path.lineTo(79, 158)
    path.lineTo(79, 176)
    path.arcTo(QRectF(79, 157, 38, 38), 180, 180)  # inner bowl, (79,176) -> (117,176)
    path.closeSubpath()
    return path


def _journal_paths() -> tuple[QPainterPath, QPainterPath, QPainterPath]:
    cover = QPainterPath(QPointF(128, 74))
    cover.cubicTo(106, 62, 70, 58, 32, 64)
    cover.lineTo(32, 134)
    cover.cubicTo(70, 128, 106, 132, 128, 144)
    cover.cubicTo(150, 132, 186, 128, 224, 134)
    cover.lineTo(224, 64)
    cover.cubicTo(186, 58, 150, 62, 128, 74)
    cover.closeSubpath()
    left = QPainterPath(QPointF(128, 66))
    left.cubicTo(108, 54, 72, 50, 40, 56)
    left.lineTo(40, 124)
    left.cubicTo(72, 118, 108, 122, 128, 134)
    left.closeSubpath()
    right = QPainterPath(QPointF(128, 66))
    right.cubicTo(148, 54, 184, 50, 216, 56)
    right.lineTo(216, 124)
    right.cubicTo(184, 118, 148, 122, 128, 134)
    right.closeSubpath()
    return cover, left, right


def paint_logo_mark(p: QPainter, size: int) -> None:
    """The ribbon J and the open journal without the tile, on the 256-unit grid of the logo (the caller sets the
    transform). ``size`` is the rendered pixel size of that grid: it decides which details are drawn."""
    p.save()
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(_vertical(_RIBBON_TOP, _RIBBON_BOTTOM, 80, 220))
    p.drawPath(ribbon_path())
    if size >= 64:
        shine = QPainterPath(QPointF(117, 96))
        shine.lineTo(129, 96)
        shine.lineTo(129, 176)
        shine.arcTo(QRectF(67, 145, 62, 62), 0, -180)
        p.setPen(QPen(QColor(255, 255, 255, 41), 2))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPath(shine)
    cover, left, right = _journal_paths()
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(_vertical(_COVER_TOP, _COVER_BOTTOM, 50, 220))
    p.drawPath(cover)
    p.setBrush(_vertical(_PAGE_TOP, _PAGE_BOTTOM, 40, 200))
    p.drawPath(left)
    p.drawPath(right)
    if size >= 32:
        p.setPen(QPen(_GUTTER, 2))
        p.drawLine(QPointF(128, 66), QPointF(128, 134))
    if size >= 48:
        pen = QPen(_PAGE_LINE, 4)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        for dy in (0, 14, 28):
            line = QPainterPath(QPointF(54, 74 + dy))
            line.cubicTo(76, 70 + dy, 98, 72 + dy, 114, 78 + dy)
            p.drawPath(line)
            line = QPainterPath(QPointF(156, 78 + dy))
            line.cubicTo(170, 72 + dy, 188, 70 + dy, 202, 74 + dy)
            p.drawPath(line)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(_ENTRY_MARK)
        for dy in (0, 14, 28):
            p.drawRoundedRect(QRectF(142, 73 + dy, 7, 7), 1.2, 1.2)
    p.restore()


def paint_tile(p: QPainter, rect: QRectF, radius: float, *, border: bool = True) -> None:
    """The navy tile of the logo (vertical gradient, thin light edge)."""
    p.save()
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(_vertical(_TILE_TOP, _TILE_BOTTOM, rect.top(), rect.bottom()))
    p.drawRoundedRect(rect, radius, radius)
    if border:
        width = max(1.0, rect.width() / 120)
        p.setPen(QPen(QColor(255, 255, 255, 31), width))
        p.setBrush(Qt.BrushStyle.NoBrush)
        inner = rect.adjusted(width / 2, width / 2, -width / 2, -width / 2)
        p.drawRoundedRect(inner, radius - width / 2, radius - width / 2)
    p.restore()


def paint_app_icon(p: QPainter, size: int) -> None:
    """The JRef logo -- an open journal whose amber bookmark ribbon curls into the J -- drawn as vectors on a
    256-unit grid (the master artwork is ``packaging/branding/jref_logo_ribbon.svg``). It stays legible from
    16 px (title bar, taskbar) to 1024 px; below 48 px the text lines on the pages are left out."""
    p.save()
    p.scale(size / 256.0, size / 256.0)
    paint_tile(p, QRectF(8, 8, 240, 240), 56, border=size >= 32)
    paint_logo_mark(p, size)
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
    """Application icon (window, taskbar, .ico): the logo at every standard size."""
    icon = QIcon()
    for size in APP_ICON_SIZES:
        icon.addPixmap(QPixmap.fromImage(app_icon_image(size)))
    return icon


def brand_pixmap(size: int, *, with_name: bool = True) -> QPixmap:
    """Logo artwork for dialogs (About, Welcome): the tile with the name, or the emblem alone."""
    source = QPixmap(str(ASSETS_DIR / ("jref_tile.png" if with_name else "jref_glyph.png")))
    ratio = QGuiApplication.primaryScreen().devicePixelRatio() if QGuiApplication.primaryScreen() else 1.0
    if source.isNull():  # pragma: no cover - broken installation: fall back to the vector logo
        source = QPixmap.fromImage(app_icon_image(round(size * ratio)))
    pix = source.scaled(
        round(size * ratio),
        round(size * ratio),
        Qt.AspectRatioMode.KeepAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    )
    pix.setDevicePixelRatio(ratio)
    return pix
