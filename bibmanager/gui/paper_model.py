"""Reference list: table model, filter/sort proxy, the key-first row delegate (rows view) and the cell delegates
of the table view."""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, date, datetime
from enum import IntEnum
from typing import Any

from PySide6.QtCore import (
    QAbstractItemModel,
    QAbstractTableModel,
    QEvent,
    QMimeData,
    QModelIndex,
    QPersistentModelIndex,
    QRect,
    QRectF,
    QSize,
    QSortFilterProxyModel,
    Qt,
    Signal,
)
from PySide6.QtGui import QFont, QFontMetrics, QIcon, QMouseEvent, QPainter, QPen, QTextLayout
from PySide6.QtWidgets import QStyle, QStyledItemDelegate, QStyleOptionViewItem, QWidget
from unidecode import unidecode

from ..core.models import Paper, split_list_field
from ..core.query import Query, SearchRecord, build_record, matches, parse_query
from . import icons, theme

PAPER_MIME = "application/x-bibmanager-paper-ids"
SORT_ROLE = Qt.ItemDataRole.UserRole + 1
PAPER_ID_ROLE = Qt.ItemDataRole.UserRole + 2
PAPER_ROLE = Qt.ItemDataRole.UserRole + 3

Index = QModelIndex | QPersistentModelIndex


class Col(IntEnum):
    FAVORITE = 0
    READ = 1
    FILE = 2
    AUTHORS = 3
    TITLE = 4
    YEAR = 5
    PUBLISHED_IN = 6
    ADDED = 7
    CITATION_KEY = 8
    FOLDERS = 9
    DOI = 10
    REFERENCE = 11  # the rows view: key, title, authors, venue and status glyphs painted in one cell
    OPENED = 12  # when the PDF was last opened from JRef


HEADERS = {
    Col.FAVORITE: "",
    Col.READ: "",
    Col.FILE: "",
    Col.AUTHORS: "Authors",
    Col.TITLE: "Title",
    Col.YEAR: "Year",
    Col.PUBLISHED_IN: "Venue",
    Col.ADDED: "Added",
    Col.CITATION_KEY: "Key",
    Col.FOLDERS: "Projects",
    Col.DOI: "DOI",
    Col.REFERENCE: "Reference",
    Col.OPENED: "Opened",
}
COLUMN_NAMES = {
    **HEADERS,
    Col.FAVORITE: "Starred",
    Col.READ: "Unread marker",
    Col.FILE: "Files",
}
ICON_COLUMNS = frozenset({Col.FAVORITE, Col.READ, Col.FILE})
# Table view: what is shown by default and in which order (the rows view shows Col.REFERENCE alone).
DEFAULT_HIDDEN = frozenset({Col.READ, Col.FOLDERS, Col.DOI, Col.REFERENCE, Col.OPENED})
DEFAULT_ORDER = (
    Col.CITATION_KEY, Col.TITLE, Col.AUTHORS, Col.YEAR, Col.PUBLISHED_IN, Col.ADDED, Col.FILE, Col.FAVORITE,
    Col.READ, Col.FOLDERS, Col.DOI, Col.OPENED, Col.REFERENCE,
)  # fmt: skip
TABLE_COLUMNS = tuple(c for c in DEFAULT_ORDER if c != Col.REFERENCE)
DEFAULT_WIDTHS = {
    Col.FAVORITE: 32,
    Col.READ: 24,
    Col.FILE: 34,
    Col.AUTHORS: 160,
    Col.TITLE: 380,
    Col.YEAR: 56,
    Col.PUBLISHED_IN: 130,
    Col.ADDED: 72,
    Col.CITATION_KEY: 160,
    Col.FOLDERS: 150,
    Col.DOI: 170,
    Col.REFERENCE: 640,
    Col.OPENED: 78,
}

FACET_KINDS = ("authors", "keywords", "tags", "publications")


def _fold(text: str) -> str:
    return unidecode(text or "").casefold()


def parse_timestamp(value: str) -> datetime | None:
    if not value:
        return None
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=UTC)


def format_added(created_at: str, today: date | None = None) -> str:
    """``Oct 1`` for the current year, ``Oct 1 2024`` otherwise (local time)."""
    stamp = parse_timestamp(created_at)
    if stamp is None:
        return ""
    local = stamp.astimezone()
    today = today or date.today()
    label = f"{local:%b} {local.day}"
    return label if local.year == today.year else f"{label} {local.year}"


def facet_values(paper: Paper, kind: str) -> list[str]:
    if kind == "authors":
        return [f"{a.family}, {a.given}".rstrip(", ") if a.family else a.literal for a in paper.meta.authors]
    if kind == "keywords":
        return split_list_field(paper.meta.keywords)
    if kind == "tags":
        return split_list_field(paper.tags)
    if kind == "publications":
        return [paper.meta.journal] if paper.meta.journal else []
    raise ValueError(kind)


class PaperTableModel(QAbstractTableModel):
    def __init__(self, parent: Any = None) -> None:
        super().__init__(parent)
        self._papers: list[Paper] = []
        self._folders: dict[int, list[str]] = {}
        self._row_by_id: dict[int, int] = {}
        self._records: list[SearchRecord] = []

    # ------------------------------------------------------------------ data access
    def set_papers(self, papers: list[Paper], folders: dict[int, list[str]] | None = None) -> None:
        self.beginResetModel()
        self._papers = list(papers)
        self._folders = folders or {}
        self._row_by_id = {p.id: row for row, p in enumerate(self._papers)}
        self._records = [build_record(p, self._folders.get(p.id, [])) for p in self._papers]
        self.endResetModel()

    def paper(self, row: int) -> Paper:
        return self._papers[row]

    def papers(self) -> list[Paper]:
        return list(self._papers)

    def row_of(self, paper_id: int) -> int | None:
        return self._row_by_id.get(paper_id)

    def haystack(self, row: int) -> str:
        return self._records[row].all_text

    def record(self, row: int) -> SearchRecord:
        return self._records[row]

    def folders_of(self, paper_id: int) -> list[str]:
        return self._folders.get(paper_id, [])

    def set_folders(self, folders: dict[int, list[str]]) -> None:
        """New project memberships (search records and the Projects column follow)."""
        self._folders = folders or {}
        self._records = [build_record(p, self._folders.get(p.id, [])) for p in self._papers]
        if self._papers:
            self.dataChanged.emit(self.index(0, 0), self.index(len(self._papers) - 1, len(Col) - 1))

    def replace_paper(self, paper: Paper) -> None:
        row = self._row_by_id.get(paper.id)
        if row is None:
            return
        self._papers[row] = paper
        self._records[row] = build_record(paper, self._folders.get(paper.id, []))
        self.dataChanged.emit(self.index(row, 0), self.index(row, len(Col) - 1))

    # ------------------------------------------------------------------ Qt model API
    def rowCount(self, parent: Index = QModelIndex()) -> int:  # noqa: B008
        return 0 if parent.isValid() else len(self._papers)

    def columnCount(self, parent: Index = QModelIndex()) -> int:  # noqa: B008
        return 0 if parent.isValid() else len(Col)

    def data(self, index: Index, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid():
            return None
        paper = self._papers[index.row()]
        col = Col(index.column())
        meta = paper.meta
        if role == Qt.ItemDataRole.DisplayRole:
            return {
                Col.AUTHORS: meta.authors_inverted(),
                Col.TITLE: meta.title,
                Col.YEAR: str(meta.year) if meta.year is not None else "",
                Col.PUBLISHED_IN: meta.journal,
                Col.ADDED: format_added(paper.created_at),
                Col.CITATION_KEY: paper.citation_key,
                Col.FOLDERS: ", ".join(self._folders.get(paper.id, [])),
                Col.DOI: meta.doi,
                Col.REFERENCE: meta.title,
                Col.OPENED: format_added(paper.last_opened_at),
            }.get(col)
        if role == Qt.ItemDataRole.DecorationRole:
            if col == Col.FAVORITE:
                return icons.star(paper.favorite)
            if col == Col.READ:
                return icons.read_dot(not paper.is_read)
            if col == Col.FILE:
                if paper.has_pdf:
                    return icons.pdf_with_supplements() if paper.attachments else icons.pdf_file()
                return icons.paperclip() if paper.attachments else None
            return None
        if role == SORT_ROLE:
            return {
                Col.FAVORITE: int(paper.favorite),
                Col.READ: int(not paper.is_read),
                Col.FILE: (0 if paper.has_pdf else 2) - (1 if paper.attachments else 0),
                Col.AUTHORS: _fold(meta.authors_inverted()),
                Col.TITLE: _fold(meta.title),
                Col.YEAR: meta.year if meta.year is not None else 0,
                Col.PUBLISHED_IN: _fold(meta.journal),
                Col.ADDED: paper.created_at,
                Col.CITATION_KEY: paper.citation_key,
                Col.FOLDERS: _fold(", ".join(self._folders.get(paper.id, []))),
                Col.DOI: meta.doi,
                Col.REFERENCE: _fold(meta.title),
                Col.OPENED: paper.last_opened_at or "",
            }[col]
        if role == PAPER_ID_ROLE:
            return paper.id
        if role == PAPER_ROLE:
            return paper
        if role == Qt.ItemDataRole.ToolTipRole:
            if col == Col.FAVORITE:
                return "Starred" if paper.favorite else "Click to star"
            if col == Col.READ:
                return "Unread" if not paper.is_read else "Read"
            if col == Col.FILE:
                n = paper.attachments
                extra = f"\n+ {n} supplementary file{'s' if n != 1 else ''}" if n else ""
                if not paper.has_pdf:
                    return ("No PDF attached" + extra) if n else "No PDF attached"
                main = f"Linked file: {paper.relative_pdf_path}" if paper.pdf_linked else paper.relative_pdf_path
                return f"{main}{extra}"
            if col in (Col.TITLE, Col.AUTHORS, Col.PUBLISHED_IN, Col.FOLDERS):
                return self.data(index, Qt.ItemDataRole.DisplayRole) or None
            if col == Col.REFERENCE:
                folders = self._folders.get(paper.id, [])
                lines = [meta.title, meta.authors_inverted()]
                if folders:
                    lines.append("Projects: " + ", ".join(folders))
                return "\n".join(line for line in lines if line) or None
            return None
        if role == Qt.ItemDataRole.TextAlignmentRole:
            if col == Col.YEAR:
                return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop)
            return int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        return None

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if orientation != Qt.Orientation.Horizontal:
            return None
        col = Col(section)
        if role == Qt.ItemDataRole.DisplayRole:
            return HEADERS[col]
        if role == Qt.ItemDataRole.DecorationRole:
            return {Col.FAVORITE: icons.star(False), Col.READ: icons.unread_header(), Col.FILE: icons.document()}.get(
                col
            )
        if role == Qt.ItemDataRole.ToolTipRole:
            return COLUMN_NAMES[col]
        return None

    def flags(self, index: Index) -> Qt.ItemFlag:
        base = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        return base | Qt.ItemFlag.ItemIsDragEnabled if index.isValid() else base

    # ------------------------------------------------------------------ drag source
    def mimeTypes(self) -> list[str]:
        return [PAPER_MIME, "text/plain"]

    def mimeData(self, indexes: Sequence[QModelIndex]) -> QMimeData:
        rows = sorted({i.row() for i in indexes if i.isValid()})
        papers = [self._papers[r] for r in rows]
        mime = QMimeData()
        mime.setData(PAPER_MIME, json.dumps([p.id for p in papers]).encode())
        mime.setText("\\cite{" + ",".join(p.citation_key for p in papers) + "}")  # drop into a .tex editor
        return mime

    def supportedDragActions(self) -> Qt.DropAction:
        return Qt.DropAction.CopyAction | Qt.DropAction.LinkAction


def paper_ids_from_mime(mime: QMimeData) -> list[int]:
    if not mime.hasFormat(PAPER_MIME):
        return []
    try:
        return [int(x) for x in json.loads(bytes(mime.data(PAPER_MIME).data()).decode())]
    except (ValueError, TypeError):
        return []


class PaperFilterProxy(QSortFilterProxyModel):
    """Everything that narrows the list, combined with AND: the search box query (``core.query``: free words in a
    scope + field qualifiers), the full-text hit set, the filter chips (a query of flags and a year range), the
    saved search being shown, facet values chosen from the Author / Venue / Tag chips, and an explicit id set
    (e.g. "in the project, never cited")."""

    def __init__(self, parent: Any = None) -> None:
        super().__init__(parent)
        self.setSortRole(SORT_ROLE)
        self.setDynamicSortFilter(True)
        self._query = Query()
        self._scope = "all"
        self._skip_free_text = False
        self._ids: set[int] | None = None
        self._restrict: set[int] | None = None
        self._chips = Query()
        self._saved = Query()
        self._facets: dict[str, str] = {}

    # Qt >= 6.10 wants begin/endFilterChange around parameter changes; older Qt uses invalidateRowsFilter.
    def _begin(self) -> None:
        if hasattr(self, "beginFilterChange"):
            self.beginFilterChange()

    def _end(self) -> None:
        if hasattr(self, "endFilterChange"):
            self.endFilterChange(QSortFilterProxyModel.Direction.Rows)
        else:  # pragma: no cover - Qt < 6.10
            self.invalidateRowsFilter()

    def set_text(self, text: str) -> None:
        self.set_query(parse_query(text))

    def set_query(self, query: Query, scope: str = "all", *, skip_free_text: bool = False) -> None:
        """``skip_free_text``: free words were already answered by full-text search (``set_ids``)."""
        self._begin()
        self._query, self._scope, self._skip_free_text = query, scope, skip_free_text
        self._end()

    def set_ids(self, ids: set[int] | None) -> None:
        self._begin()
        self._ids = ids
        self._end()

    def set_facet(self, kind: str | None, value: str | None) -> None:
        """Set (or clear, with ``value=None``) one facet; the other facets are kept."""
        self._begin()
        if kind:
            if value:
                self._facets[kind] = value
            else:
                self._facets.pop(kind, None)
        self._end()

    def set_facets(self, facets: dict[str, str]) -> None:
        self._begin()
        self._facets = {k: v for k, v in facets.items() if k and v}
        self._end()

    def facets(self) -> dict[str, str]:
        return dict(self._facets)

    def set_chip_query(self, query: Query) -> None:
        self._begin()
        self._chips = query
        self._end()

    def set_saved_query(self, query: Query) -> None:
        self._begin()
        self._saved = query
        self._end()

    def set_restrict(self, ids: set[int] | None) -> None:
        self._begin()
        self._restrict = set(ids) if ids is not None else None
        self._end()

    @property
    def restricted(self) -> bool:
        return self._restrict is not None

    def filterAcceptsRow(self, source_row: int, source_parent: Index) -> bool:
        model: PaperTableModel = self.sourceModel()  # type: ignore[assignment]
        paper = model.paper(source_row)
        if self._ids is not None and paper.id not in self._ids:
            return False
        if self._restrict is not None and paper.id not in self._restrict:
            return False
        for kind, value in self._facets.items():
            if value not in facet_values(paper, kind):
                return False
        record = model.record(source_row)
        if self._chips and not matches(self._chips, record):
            return False
        if self._saved and not matches(self._saved, record):
            return False
        if self._query:
            return matches(self._query, record, self._scope, skip_free_text=self._skip_free_text)
        return True


# --------------------------------------------------------------------------- delegates
def _paint_row_background(painter: QPainter, option: QStyleOptionViewItem, index: Index) -> None:
    """One rounded hover/selection shape across all visible columns (amber tint with a fine edge when selected)."""
    view = option.widget
    selected = bool(option.state & QStyle.StateFlag.State_Selected)
    hovered = view is not None and getattr(view, "hover_row", -1) == index.row()
    if not (selected or hovered):
        return
    t = theme.current()
    header = view.horizontalHeader() if view is not None and hasattr(view, "horizontalHeader") else None
    first = last = True
    if header is not None:
        visible = [
            header.logicalIndex(v) for v in range(header.count()) if not header.isSectionHidden(header.logicalIndex(v))
        ]
        first = bool(visible) and visible[0] == index.column()
        last = bool(visible) and visible[-1] == index.column()
    radius = 8.0
    rect = QRectF(option.rect).adjusted(0, 2, 0, -2)
    left = rect.left() + 6 if first else rect.left() - radius - 1
    right = rect.right() - 6 if last else rect.right() + radius + 1
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setClipRect(option.rect)
    shape = QRectF(left, rect.top(), right - left, rect.height())
    if selected:
        painter.setPen(QPen(theme.qcolor(t.selected_border), 1))
        painter.setBrush(theme.qcolor(t.selected))
        painter.drawRoundedRect(shape.adjusted(0.5, 0.5, -0.5, -0.5), radius, radius)
    else:
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(theme.qcolor(t.hover))
        painter.drawRoundedRect(shape, radius, radius)
    painter.restore()


def _plain_option(option: QStyleOptionViewItem, index: Index, delegate: QStyledItemDelegate) -> QStyleOptionViewItem:
    opt = QStyleOptionViewItem(option)
    delegate.initStyleOption(opt, index)
    opt.state &= ~(QStyle.StateFlag.State_Selected | QStyle.StateFlag.State_MouseOver | QStyle.StateFlag.State_HasFocus)
    return opt


def short_authors(paper: Paper, limit: int = 3) -> str:
    """``Reese, Govindjee`` / ``Miehe, Welschinger, Hofacker`` / ``Ambati et al.`` for the rows view."""
    names = [a.family or a.literal for a in paper.meta.authors if a.family or a.literal]
    if not names:
        return ""
    return ", ".join(names) if len(names) <= limit else f"{names[0]} et al."


class TwoLineDelegate(QStyledItemDelegate):
    """Table view cell: top-aligned text wrapped on at most ``max_lines`` lines, last line elided; the key column in
    the monospace font, the title bold while the reference is unread."""

    def __init__(self, parent: QWidget | None = None, max_lines: int = 2) -> None:
        super().__init__(parent)
        self.max_lines = max_lines

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: Index) -> None:
        _paint_row_background(painter, option, index)
        opt = _plain_option(option, index, self)
        text = opt.text
        if not text:
            return
        t = theme.current()
        font = QFont(opt.font)
        column = index.column()
        paper = index.data(PAPER_ROLE)
        if column == Col.CITATION_KEY:
            font = theme.mono_font(max(7.5, opt.font.pointSizeF() * 0.9))
        elif column == Col.TITLE and paper is not None and not paper.is_read:
            font.setWeight(QFont.Weight.DemiBold)
        opt.font = font
        rect = QRectF(opt.rect.adjusted(8, 8, -6, -4))
        metrics = QFontMetrics(font)
        lines = self.wrap_lines(text, opt, rect.width())
        painter.save()
        painter.setFont(font)
        secondary = column in (Col.YEAR, Col.PUBLISHED_IN, Col.ADDED, Col.FOLDERS, Col.DOI)
        color = t.key_text if column == Col.CITATION_KEY else t.text_secondary if secondary else t.text
        painter.setPen(theme.qcolor(color))
        alignment = index.data(Qt.ItemDataRole.TextAlignmentRole)
        right = bool(alignment) and bool(int(alignment) & int(Qt.AlignmentFlag.AlignRight))
        h_align = Qt.AlignmentFlag.AlignRight if right else Qt.AlignmentFlag.AlignLeft
        for i, line in enumerate(lines):
            line_rect = QRectF(rect.left(), rect.top() + i * metrics.lineSpacing(), rect.width(), metrics.lineSpacing())
            painter.drawText(line_rect, int(h_align | Qt.AlignmentFlag.AlignVCenter), line)
        painter.restore()

    def wrap_lines(self, text: str, opt: QStyleOptionViewItem, width: float) -> list[str]:
        metrics = QFontMetrics(opt.font)
        text = " ".join(text.split())
        layout = QTextLayout(text, opt.font)
        layout.beginLayout()
        lines: list[str] = []
        while True:
            line = layout.createLine()
            if not line.isValid():
                break
            line.setLineWidth(width)
            start = line.textStart()
            if len(lines) == self.max_lines - 1:
                lines.append(metrics.elidedText(text[start:], Qt.TextElideMode.ElideRight, int(width)))
                break
            lines.append(text[start : start + line.textLength()].rstrip())
        layout.endLayout()
        return lines


class IconDelegate(QStyledItemDelegate):
    """Centred 16 px decoration (star / unread dot / PDF glyph), no text."""

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: Index) -> None:
        _paint_row_background(painter, option, index)
        icon = index.data(Qt.ItemDataRole.DecorationRole)
        if isinstance(icon, QIcon) and not icon.isNull():
            size = 16
            x = option.rect.left() + (option.rect.width() - size) // 2
            y = option.rect.top() + 10
            icon.paint(painter, x, y, size, size)


class ReferenceDelegate(QStyledItemDelegate):
    """Rows view: the citation key in a badge, the title (serif, bold while unread) over authors · venue, and
    the status glyphs on the right (notes, supplementary files, PDF, star). When the list is sorted by year,
    the year is printed in a left gutter on the first row of each year."""

    starClicked = Signal(int)  # paper id

    PAD_X, PAD_Y, GUTTER, KEY_W, GLYPH = 14, 10, 52, 184, 16

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.year_gutter = False

    # ------------------------------------------------------------------ geometry
    def _fonts(self, base: QFont) -> tuple[QFont, QFont, QFont]:
        title = theme.serif_font(base.pointSizeF() * 1.14)
        meta = QFont(base)
        meta.setPointSizeF(base.pointSizeF() * 0.93)
        key = theme.mono_font(base.pointSizeF() * 0.88)
        return title, meta, key

    def row_height(self, base: QFont) -> int:
        title, meta, _key = self._fonts(base)
        bold = QFont(title)
        bold.setWeight(QFont.Weight.DemiBold)
        return QFontMetrics(bold).lineSpacing() + QFontMetrics(meta).lineSpacing() + 2 * self.PAD_Y + 6

    def sizeHint(self, option: QStyleOptionViewItem, index: Index) -> QSize:
        return QSize(600, self.row_height(option.font))

    def _star_rect(self, rect: QRect) -> QRect:
        return QRect(rect.right() - self.PAD_X - self.GLYPH, rect.top() + self.PAD_Y + 2, self.GLYPH, self.GLYPH)

    def star_rect(self, rect: QRect) -> QRect:
        return self._star_rect(rect).adjusted(-4, -4, 4, 4)

    # ------------------------------------------------------------------ painting
    def _first_of_year(self, index: Index, paper: Paper) -> bool:
        if index.row() == 0:
            return True
        model = index.model()
        previous = model.index(index.row() - 1, index.column()).data(PAPER_ROLE) if model is not None else None
        return previous is None or previous.meta.year != paper.meta.year

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: Index) -> None:
        _paint_row_background(painter, option, index)
        paper: Paper | None = index.data(PAPER_ROLE)
        if paper is None:
            return
        t = theme.current()
        rect = option.rect
        title_font, meta_font, key_font = self._fonts(option.font)
        if not paper.is_read:
            title_font.setWeight(QFont.Weight.DemiBold)
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        x = rect.left() + self.PAD_X
        top = rect.top() + self.PAD_Y
        if self.year_gutter:
            if self._first_of_year(index, paper):
                painter.setFont(meta_font)
                painter.setPen(theme.qcolor(t.text_secondary))
                label = str(paper.meta.year) if paper.meta.year is not None else "n.d."
                painter.drawText(QRectF(x, top + 2, self.GUTTER - 8, 20), int(Qt.AlignmentFlag.AlignLeft), label)
            x += self.GUTTER
        # citation key badge
        key_metrics = QFontMetrics(key_font)
        key_text = key_metrics.elidedText(paper.citation_key, Qt.TextElideMode.ElideMiddle, self.KEY_W - 18)
        badge = QRectF(x, top + 1, key_metrics.horizontalAdvance(key_text) + 16, key_metrics.height() + 6)
        painter.setPen(QPen(theme.qcolor(t.key_border), 1))
        painter.setBrush(theme.qcolor(t.key_bg))
        painter.drawRoundedRect(badge.adjusted(0.5, 0.5, -0.5, -0.5), 6, 6)
        painter.setFont(key_font)
        painter.setPen(theme.qcolor(t.key_text))
        painter.drawText(badge, int(Qt.AlignmentFlag.AlignCenter), key_text)
        x += self.KEY_W
        # status glyphs, right to left
        right = rect.right() - self.PAD_X
        glyph_top = top + 2
        star = self._star_rect(rect)
        icons.star(paper.favorite).paint(painter, star)
        right = star.left() - 10
        if paper.has_pdf:
            icons.pdf_file().paint(painter, QRect(right - self.GLYPH, glyph_top, self.GLYPH, self.GLYPH))
            right -= self.GLYPH + 10
        if paper.attachments:
            painter.setFont(meta_font)
            painter.setPen(theme.qcolor(t.text_secondary))
            count = str(paper.attachments)
            width = QFontMetrics(meta_font).horizontalAdvance(count)
            painter.drawText(QRectF(right - width, glyph_top - 1, width, self.GLYPH + 2),
                             int(Qt.AlignmentFlag.AlignVCenter), count)  # fmt: skip
            right -= width + 2
            icons.paperclip().paint(painter, QRect(right - self.GLYPH, glyph_top, self.GLYPH, self.GLYPH))
            right -= self.GLYPH + 10
        if paper.notes.strip():
            icons.note().paint(painter, QRect(right - self.GLYPH, glyph_top, self.GLYPH, self.GLYPH))
            right -= self.GLYPH + 10
        # title and meta line
        width = max(40, right - x - 4)
        title_metrics = QFontMetrics(title_font)
        title = title_metrics.elidedText(" ".join((paper.meta.title or "(untitled)").split()),
                                         Qt.TextElideMode.ElideRight, width)  # fmt: skip
        painter.setFont(title_font)
        painter.setPen(theme.qcolor(t.text))
        painter.drawText(QRectF(x, top - 1, width, title_metrics.lineSpacing()),
                         int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), title)  # fmt: skip
        parts = [short_authors(paper), paper.meta.journal, str(paper.meta.year) if paper.meta.year else ""]
        meta_text = " · ".join(p for p in parts if p)
        meta_metrics = QFontMetrics(meta_font)
        painter.setFont(meta_font)
        painter.setPen(theme.qcolor(t.text_secondary))
        painter.drawText(QRectF(x, top + title_metrics.lineSpacing() + 2, width, meta_metrics.lineSpacing()),
                         int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                         meta_metrics.elidedText(meta_text, Qt.TextElideMode.ElideRight, width))  # fmt: skip
        painter.restore()

    def editorEvent(
        self, event: QEvent, model: QAbstractItemModel, option: QStyleOptionViewItem, index: Index
    ) -> bool:
        if event.type() == QEvent.Type.MouseButtonRelease and isinstance(event, QMouseEvent):
            if event.button() == Qt.MouseButton.LeftButton and self.star_rect(option.rect).contains(
                event.position().toPoint()
            ):
                pid = index.data(PAPER_ID_ROLE)
                if pid is not None:
                    self.starClicked.emit(int(pid))
                    return True
        return super().editorEvent(event, model, option, index)
