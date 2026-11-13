"""Document list: table model, filter/sort proxy and the two-line row delegate."""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, date, datetime
from enum import IntEnum
from typing import Any

from PySide6.QtCore import (
    QAbstractTableModel,
    QMimeData,
    QModelIndex,
    QPersistentModelIndex,
    QRectF,
    QSortFilterProxyModel,
    Qt,
)
from PySide6.QtGui import QFontMetrics, QIcon, QPainter, QTextLayout
from PySide6.QtWidgets import QStyle, QStyledItemDelegate, QStyleOptionViewItem, QWidget
from unidecode import unidecode

from ..core.models import Paper, split_list_field
from ..core.query import Query, SearchRecord, build_record, matches, parse_query
from . import icons, theme

PAPER_MIME = "application/x-bibmanager-paper-ids"
SORT_ROLE = Qt.ItemDataRole.UserRole + 1
PAPER_ID_ROLE = Qt.ItemDataRole.UserRole + 2

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


HEADERS = {
    Col.FAVORITE: "",
    Col.READ: "",
    Col.FILE: "",
    Col.AUTHORS: "Authors",
    Col.TITLE: "Title",
    Col.YEAR: "Year",
    Col.PUBLISHED_IN: "Published in",
    Col.ADDED: "Added",
    Col.CITATION_KEY: "Citation key",
    Col.FOLDERS: "Folders",
    Col.DOI: "DOI",
}
COLUMN_NAMES = {
    **HEADERS,
    Col.FAVORITE: "Favorite",
    Col.READ: "Read status",
    Col.FILE: "File attached",
}
ICON_COLUMNS = frozenset({Col.FAVORITE, Col.READ, Col.FILE})
DEFAULT_HIDDEN = frozenset({Col.CITATION_KEY, Col.FOLDERS, Col.DOI})
DEFAULT_WIDTHS = {
    Col.FAVORITE: 30,
    Col.READ: 22,
    Col.FILE: 28,
    Col.AUTHORS: 210,
    Col.TITLE: 380,
    Col.YEAR: 54,
    Col.PUBLISHED_IN: 130,
    Col.ADDED: 74,
    Col.CITATION_KEY: 170,
    Col.FOLDERS: 150,
    Col.DOI: 170,
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
            }[col]
        if role == PAPER_ID_ROLE:
            return paper.id
        if role == Qt.ItemDataRole.ToolTipRole:
            if col == Col.FAVORITE:
                return "Favorite" if paper.favorite else "Click to mark as favorite"
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
    """Search query (``core.query``: free words in a scope + field qualifiers), full-text hit set and
    left-pane facet, combined with AND."""

    def __init__(self, parent: Any = None) -> None:
        super().__init__(parent)
        self.setSortRole(SORT_ROLE)
        self.setDynamicSortFilter(True)
        self._query = Query()
        self._scope = "all"
        self._skip_free_text = False
        self._ids: set[int] | None = None
        self._facet: tuple[str, str] | None = None

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
        self._begin()
        self._facet = (kind, value) if kind and value else None
        self._end()

    def filterAcceptsRow(self, source_row: int, source_parent: Index) -> bool:
        model: PaperTableModel = self.sourceModel()  # type: ignore[assignment]
        paper = model.paper(source_row)
        if self._ids is not None and paper.id not in self._ids:
            return False
        if self._facet is not None and self._facet[1] not in facet_values(paper, self._facet[0]):
            return False
        if self._query:
            return matches(self._query, model.record(source_row), self._scope, skip_free_text=self._skip_free_text)
        return True


# --------------------------------------------------------------------------- delegates
def _paint_row_background(painter: QPainter, option: QStyleOptionViewItem, index: Index) -> None:
    """Fluent list row: one rounded hover/selection shape across all visible columns + accent pill."""
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
    radius = 6.0
    rect = QRectF(option.rect).adjusted(0, 2, 0, -2)
    left = rect.left() + 4 if first else rect.left() - radius
    right = rect.right() - 4 if last else rect.right() + radius
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setClipRect(option.rect)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(theme.qcolor(t.selected if selected else t.hover))
    painter.drawRoundedRect(QRectF(left, rect.top(), right - left, rect.height()), radius, radius)
    if selected and first:
        painter.setBrush(theme.qcolor(t.accent))
        painter.drawRoundedRect(QRectF(left, rect.center().y() - 8, 3, 16), 1.5, 1.5)
    painter.restore()


def _plain_option(option: QStyleOptionViewItem, index: Index, delegate: QStyledItemDelegate) -> QStyleOptionViewItem:
    opt = QStyleOptionViewItem(option)
    delegate.initStyleOption(opt, index)
    opt.state &= ~(QStyle.StateFlag.State_Selected | QStyle.StateFlag.State_MouseOver | QStyle.StateFlag.State_HasFocus)
    return opt


class TwoLineDelegate(QStyledItemDelegate):
    """Top-aligned text wrapped on at most ``max_lines`` lines, last line elided."""

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
        rect = QRectF(opt.rect.adjusted(8, 7, -6, -4))
        metrics = QFontMetrics(opt.font)
        lines = self.wrap_lines(text, opt, rect.width())
        painter.save()
        painter.setFont(opt.font)
        secondary = index.column() in (Col.YEAR, Col.PUBLISHED_IN, Col.ADDED, Col.FOLDERS, Col.DOI, Col.CITATION_KEY)
        painter.setPen(theme.qcolor(t.text_secondary if secondary else t.text))
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
            y = option.rect.top() + 9
            icon.paint(painter, x, y, size, size)
