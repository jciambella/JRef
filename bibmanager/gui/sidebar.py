"""The shelf: JRef's left column on navy -- Inbox, Library, the projects (each with the state of its .bib file),
the saved searches, and the synchronisation status of a shared library."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from enum import StrEnum
from pathlib import Path
from typing import Any

from PySide6.QtCore import QMimeData, QModelIndex, QPersistentModelIndex, QRect, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QIcon, QPainter, QPalette, QStandardItem, QStandardItemModel
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFrame,
    QHBoxLayout,
    QLabel,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

from .. import APP_NAME, APP_SUBTITLE
from ..core.models import Collection
from . import icons, theme
from .paper_model import PAPER_MIME

KIND_ROLE = Qt.ItemDataRole.UserRole + 10
COLLECTION_ROLE = Qt.ItemDataRole.UserRole + 11
COUNT_ROLE = Qt.ItemDataRole.UserRole + 12
STATUS_ROLE = Qt.ItemDataRole.UserRole + 13  # (text, state) under a project: state "ok" | "pending" | "none"
QUERY_ROLE = Qt.ItemDataRole.UserRole + 14


class NodeKind(StrEnum):
    INBOX = "inbox"  # imported references not verified yet (needs_review)
    ALL = "all"  # the library
    FOLDER = "folder"  # a project: a logical collection with its own .bib file
    SAVED = "saved"  # a saved search (COLLECTION_ROLE holds its position in the list)
    CREATE = "create"
    HEADER = "header"


SPECIAL_NODES: tuple[tuple[NodeKind, str], ...] = ((NodeKind.INBOX, "Inbox"), (NodeKind.ALL, "Library"))
NODE_TITLES = dict(SPECIAL_NODES)

# Where things may be dropped: references onto projects; PDF files onto the library, the inbox and projects.
_PAPER_TARGETS = {NodeKind.FOLDER}
_FILE_TARGETS = {NodeKind.ALL, NodeKind.INBOX, NodeKind.FOLDER}
_COUNTED = {NodeKind.INBOX, NodeKind.ALL, NodeKind.FOLDER}

Node = tuple[NodeKind, int | None]
DropHandler = Callable[[QMimeData, NodeKind, int | None], bool]
SavedSearch = tuple[str, str, str]  # name, query, sort


def _local_pdf_paths(mime: QMimeData) -> list[Path]:
    paths = [Path(u.toLocalFile()) for u in mime.urls() if u.isLocalFile()]
    return [p for p in paths if p.is_dir() or p.suffix.lower() == ".pdf"]


class CollectionModel(QStandardItemModel):
    """Drop-aware model: decides drop targets and hands accepted drops to ``drop_handler``."""

    def __init__(self, parent: Any = None) -> None:
        super().__init__(parent)
        self.drop_handler: DropHandler | None = None

    def mimeTypes(self) -> list[str]:
        return [PAPER_MIME, "text/uri-list"]

    def supportedDropActions(self) -> Qt.DropAction:
        return Qt.DropAction.CopyAction | Qt.DropAction.MoveAction | Qt.DropAction.LinkAction

    @staticmethod
    def node_of(index: QModelIndex | QPersistentModelIndex) -> Node | None:
        if not index.isValid():
            return None
        kind = index.data(KIND_ROLE)
        return (NodeKind(kind), index.data(COLLECTION_ROLE)) if kind else None

    def canDropMimeData(self, data: QMimeData, action: Qt.DropAction, row: int, column: int, parent: Any) -> bool:
        node = self.node_of(parent)
        if node is None:
            return False
        if data.hasFormat(PAPER_MIME):
            return node[0] in _PAPER_TARGETS
        return node[0] in _FILE_TARGETS and bool(_local_pdf_paths(data))

    def dropMimeData(self, data: QMimeData, action: Qt.DropAction, row: int, column: int, parent: Any) -> bool:
        if not self.canDropMimeData(data, action, row, column, parent) or self.drop_handler is None:
            return False
        node = self.node_of(parent)
        assert node is not None
        self.drop_handler(data, node[0], node[1])
        return False  # never let the view insert items: membership lives in SQLite


def count_key(kind: NodeKind | str, collection_id: int | None = None) -> str:
    if kind == NodeKind.FOLDER:
        return f"folder:{collection_id}"
    if kind == NodeKind.INBOX:
        return "review"
    return str(kind)


class ShelfDelegate(QStyledItemDelegate):
    """Shelf item on navy: rounded hover/selection, icon, label (two lines for a project: name and .bib state),
    count on the right (an amber pill for the inbox)."""

    ROW = 36
    PROJECT = 46
    HEADER = 34

    def sizeHint(self, option: QStyleOptionViewItem, index: QModelIndex | QPersistentModelIndex) -> QSize:
        kind = index.data(KIND_ROLE)
        if kind == NodeKind.HEADER:
            height = self.HEADER
        elif kind == NodeKind.FOLDER and index.data(STATUS_ROLE):
            height = self.PROJECT
        else:
            height = self.ROW
        return QSize(super().sizeHint(option, index).width(), height)

    def updateEditorGeometry(self, editor: QWidget, option: QStyleOptionViewItem, index: Any) -> None:
        editor.setGeometry(option.rect.adjusted(34, 4, -8, -(option.rect.height() - 30)))

    def paint(
        self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex | QPersistentModelIndex
    ) -> None:
        t = theme.current()
        kind = index.data(KIND_ROLE)
        text = str(index.data(Qt.ItemDataRole.DisplayRole) or "")
        rect = QRectF(option.rect)
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if kind == NodeKind.HEADER:
            font = QFont(option.font)
            font.setWeight(QFont.Weight.DemiBold)
            font.setPointSizeF(font.pointSizeF() * 0.78)
            font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 1.2)
            painter.setFont(font)
            painter.setPen(theme.qcolor(t.shelf_header))
            painter.drawText(
                rect.adjusted(12, 12, -8, 0),
                int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                text.upper(),
            )
            painter.restore()
            return
        bg = rect.adjusted(4, 2, -6, -2)
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)
        if selected or hovered:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(theme.qcolor(t.shelf_selected if selected else t.shelf_hover))
            painter.drawRoundedRect(bg, 8, 8)
        status = index.data(STATUS_ROLE)
        two_lines = kind == NodeKind.FOLDER and bool(status)
        icon = index.data(Qt.ItemDataRole.DecorationRole)
        icon_top = int(bg.top()) + 8 if two_lines else int(bg.center().y()) - 9
        if isinstance(icon, QIcon) and not icon.isNull():
            icon.paint(painter, QRect(int(bg.left()) + 9, icon_top, 18, 18))
        text_rect = bg.adjusted(36, 0, -10, 0)
        count = index.data(COUNT_ROLE)
        if count:
            label = f"{count:,}"
            metrics = QFontMetrics(option.font)
            pill = kind == NodeKind.INBOX
            width = metrics.horizontalAdvance(label) + (16 if pill else 0)
            badge = QRectF(text_rect.right() - width, (bg.top() + 6) if two_lines else bg.center().y() - 9, width, 18)
            painter.setFont(option.font)
            if pill:
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(theme.qcolor(t.accent))
                painter.drawRoundedRect(badge, 9, 9)
                bold = QFont(option.font)
                bold.setWeight(QFont.Weight.DemiBold)
                bold.setPointSizeF(option.font.pointSizeF() * 0.9)
                painter.setFont(bold)
                painter.setPen(theme.qcolor(t.accent_text))
            else:
                painter.setPen(theme.qcolor(t.shelf_muted))
            painter.drawText(badge, int(Qt.AlignmentFlag.AlignCenter), label)
            text_rect.setRight(badge.left() - 6)
        name_font = QFont(option.font)
        if two_lines:
            name_font.setWeight(QFont.Weight.Medium)
        painter.setFont(name_font)
        color = t.shelf_muted if kind == NodeKind.CREATE else t.shelf_text
        painter.setPen(theme.qcolor(color))
        metrics = QFontMetrics(name_font)
        if two_lines:
            line1 = QRectF(text_rect.left(), bg.top() + 5, text_rect.width(), metrics.lineSpacing())
            painter.drawText(line1, int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                             metrics.elidedText(text, Qt.TextElideMode.ElideRight, int(text_rect.width())))  # fmt: skip
            status_text, state = status
            small = QFont(option.font)
            small.setPointSizeF(option.font.pointSizeF() * 0.84)
            small_metrics = QFontMetrics(small)
            dot_y = line1.bottom() + 2 + small_metrics.lineSpacing() / 2
            dot = {"ok": t.ok_dot, "pending": t.accent}.get(state, t.shelf_muted)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(theme.qcolor(dot))
            painter.drawEllipse(QRectF(text_rect.left(), dot_y - 3, 6, 6))
            painter.setFont(small)
            painter.setPen(theme.qcolor(t.shelf_muted))
            line2 = QRectF(
                text_rect.left() + 11, line1.bottom() + 2, text_rect.width() - 11, small_metrics.lineSpacing()
            )
            painter.drawText(line2, int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                             small_metrics.elidedText(status_text, Qt.TextElideMode.ElideRight,
                                                      int(line2.width())))  # fmt: skip
        else:
            painter.drawText(text_rect, int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                             metrics.elidedText(text, Qt.TextElideMode.ElideRight, int(text_rect.width())))  # fmt: skip
        painter.restore()


# Kept for code written against 0.7.
NavDelegate = ShelfDelegate


class CollectionTree(QTreeView):
    nodeSelected = Signal(str, object)  # kind, collection id | saved-search position | None
    folderRenamed = Signal(int, str)
    createFolderRequested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("collections")
        self.source = CollectionModel(self)
        self.setModel(self.source)
        self.setHeaderHidden(True)
        self.setItemDelegate(ShelfDelegate(self))
        self.setIndentation(14)
        self.setMouseTracking(True)
        self.viewport().setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        palette = self.palette()  # the delegate draws selection; keep the style from filling the branch area
        for group in (QPalette.ColorGroup.Active, QPalette.ColorGroup.Inactive):
            palette.setColor(group, QPalette.ColorRole.Highlight, QColor(0, 0, 0, 0))
        self.setPalette(palette)
        self.setAnimated(True)
        self.setAcceptDrops(True)
        self.setDropIndicatorShown(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DropOnly)
        self.setDragDropOverwriteMode(True)
        self.setDefaultDropAction(Qt.DropAction.CopyAction)
        self.setEditTriggers(QAbstractItemView.EditTrigger.EditKeyPressed)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._building = False
        self._saved: list[SavedSearch] = []
        self.selectionModel().currentChanged.connect(self._on_current_changed)
        self.source.itemChanged.connect(self._on_item_changed)
        self.clicked.connect(self._on_clicked)

    # ------------------------------------------------------------------ building
    def rebuild(
        self,
        collections: Iterable[Collection],
        select: Node | None = None,
        *,
        statuses: Mapping[int, tuple[str, str]] | None = None,
        saved: Sequence[SavedSearch] = (),
    ) -> None:
        current = select or self.current_node()
        expanded = self._expanded_ids()
        statuses = statuses or {}
        self._saved = list(saved)
        self._building = True
        try:
            self.source.clear()
            root = self.source.invisibleRootItem()
            root.appendRow(self._item("Inbox", NodeKind.INBOX, None, icons.inbox()))
            root.appendRow(self._item("Library", NodeKind.ALL, None, icons.library()))
            root.appendRow(self._header("Projects"))
            items: dict[int, QStandardItem] = {}
            pending = sorted(collections, key=lambda c: c.name.casefold())
            for collection in pending:
                item = self._item(collection.name, NodeKind.FOLDER, collection.id, icons.project(), editable=True)
                status = statuses.get(collection.id)
                if status:
                    item.setData(status, STATUS_ROLE)
                item.setToolTip(f"{collection.name}\nbibtex/{collection.slug}.bib"
                                + (f"\nalso written to {collection.bib_export_path}" if collection.bib_export_path
                                   else ""))  # fmt: skip
                items[collection.id] = item
            for collection in pending:
                parent = items.get(collection.parent_id) if collection.parent_id else None
                (parent or root).appendRow(items[collection.id])
            root.appendRow(self._item("New project", NodeKind.CREATE, None, icons.add_small()))
            if self._saved:
                root.appendRow(self._header("Saved searches"))
                for position, (name, query, _sort) in enumerate(self._saved):
                    item = self._item(name, NodeKind.SAVED, position, icons.saved_search())
                    item.setData(query, QUERY_ROLE)
                    item.setToolTip(query or "(every reference)")
                    root.appendRow(item)
            for cid in expanded:
                if cid in items:
                    self.setExpanded(items[cid].index(), True)
            # Selecting during the rebuild emits nothing: the owner reloads its view explicitly.
            self.select_node(*(current or (NodeKind.ALL, None)))
        finally:
            self._building = False

    @staticmethod
    def _item(
        title: str, kind: NodeKind, collection_id: int | None, icon: Any, *, editable: bool = False
    ) -> QStandardItem:
        item = QStandardItem(icon, title)
        item.setData(str(kind), KIND_ROLE)
        item.setData(collection_id, COLLECTION_ROLE)
        flags = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        if kind in _PAPER_TARGETS | _FILE_TARGETS:
            flags |= Qt.ItemFlag.ItemIsDropEnabled
        if editable:
            flags |= Qt.ItemFlag.ItemIsEditable
        item.setFlags(flags)
        return item

    @staticmethod
    def _header(title: str) -> QStandardItem:
        item = QStandardItem(title)
        item.setData(str(NodeKind.HEADER), KIND_ROLE)
        item.setFlags(Qt.ItemFlag.ItemIsEnabled)
        return item

    def saved_search(self, position: int | None) -> SavedSearch | None:
        if position is None or not 0 <= position < len(self._saved):
            return None
        return self._saved[position]

    def update_counts(self, counts: dict[str, int]) -> None:
        """Show reference counts next to Inbox, Library and the projects (no rebuild, no signals)."""

        def walk(item: QStandardItem) -> None:
            for row in range(item.rowCount()):
                child = item.child(row)
                kind = child.data(KIND_ROLE)
                if kind in _COUNTED:
                    value = counts.get(count_key(kind, child.data(COLLECTION_ROLE)), 0)
                    if child.data(COUNT_ROLE) != value:
                        child.setData(value, COUNT_ROLE)
                walk(child)

        self._building = True
        try:
            walk(self.source.invisibleRootItem())
        finally:
            self._building = False

    def update_statuses(self, statuses: Mapping[int, tuple[str, str]]) -> None:
        """Refresh the .bib state under each project (no rebuild, no signals)."""

        def walk(item: QStandardItem) -> None:
            for row in range(item.rowCount()):
                child = item.child(row)
                if child.data(KIND_ROLE) == NodeKind.FOLDER:
                    status = statuses.get(child.data(COLLECTION_ROLE))
                    if status and child.data(STATUS_ROLE) != status:
                        child.setData(status, STATUS_ROLE)
                walk(child)

        self._building = True
        try:
            walk(self.source.invisibleRootItem())
        finally:
            self._building = False

    def _expanded_ids(self) -> set[int]:
        found: set[int] = set()

        def walk(item: QStandardItem) -> None:
            for row in range(item.rowCount()):
                child = item.child(row)
                if child.hasChildren() and self.isExpanded(child.index()):
                    found.add(child.data(COLLECTION_ROLE))
                walk(child)

        walk(self.source.invisibleRootItem())
        return found

    # ------------------------------------------------------------------ navigation
    def find_index(self, kind: NodeKind, collection_id: int | None = None) -> QModelIndex:
        def walk(item: QStandardItem) -> QModelIndex | None:
            for row in range(item.rowCount()):
                child = item.child(row)
                if child.data(KIND_ROLE) == str(kind) and (
                    kind not in (NodeKind.FOLDER, NodeKind.SAVED) or child.data(COLLECTION_ROLE) == collection_id
                ):
                    return child.index()
                found = walk(child)
                if found is not None:
                    return found
            return None

        return walk(self.source.invisibleRootItem()) or QModelIndex()

    def select_node(self, kind: NodeKind | str, collection_id: int | None = None) -> bool:
        try:
            kind = NodeKind(kind)
        except ValueError:  # a view of an earlier release (e.g. "favorites"): show the library
            kind, collection_id = NodeKind.ALL, None
        index = self.find_index(kind, collection_id)
        if not index.isValid():
            index = self.find_index(NodeKind.ALL)
        parent = index.parent()
        while parent.isValid():
            self.expand(parent)
            parent = parent.parent()
        self.setCurrentIndex(index)
        return index.isValid()

    def current_node(self) -> Node | None:
        return CollectionModel.node_of(self.currentIndex())

    def node_at(self, pos: Any) -> Node | None:
        return CollectionModel.node_of(self.indexAt(pos))

    def collection_name(self, collection_id: int) -> str:
        index = self.find_index(NodeKind.FOLDER, collection_id)
        return str(index.data()) if index.isValid() else ""

    def _on_current_changed(self, current: QModelIndex, _previous: QModelIndex) -> None:
        if self._building:
            return
        node = CollectionModel.node_of(current)
        if node is None or node[0] in (NodeKind.CREATE, NodeKind.HEADER):
            return
        self.nodeSelected.emit(str(node[0]), node[1])

    def _on_clicked(self, index: QModelIndex) -> None:
        node = CollectionModel.node_of(index)
        if node is not None and node[0] == NodeKind.CREATE:
            self.createFolderRequested.emit()

    def _on_item_changed(self, item: QStandardItem) -> None:
        if self._building or item.data(KIND_ROLE) != str(NodeKind.FOLDER):
            return
        self.folderRenamed.emit(int(item.data(COLLECTION_ROLE)), item.text())


class Shelf(QFrame):
    """The left column: brand, navigation tree and the synchronisation card."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("shelf")
        self.setMinimumWidth(220)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 16, 10, 12)
        layout.setSpacing(12)
        brand = QHBoxLayout()
        brand.setContentsMargins(8, 0, 0, 0)
        brand.setSpacing(10)
        self.logo = QLabel()
        brand.addWidget(self.logo)
        names = QVBoxLayout()
        names.setSpacing(0)
        name = QLabel(APP_NAME)
        name.setObjectName("brandName")
        subtitle = QLabel(APP_SUBTITLE)
        subtitle.setObjectName("brandSubtitle")
        names.addWidget(name)
        names.addWidget(subtitle)
        brand.addLayout(names)
        brand.addStretch(1)
        layout.addLayout(brand)
        self.tree = CollectionTree()
        layout.addWidget(self.tree, 1)
        self.card = QFrame()
        self.card.setObjectName("shelfCard")
        card = QVBoxLayout(self.card)
        card.setContentsMargins(12, 10, 12, 10)
        card.setSpacing(3)
        top = QHBoxLayout()
        top.setSpacing(8)
        self.cloud = QLabel()
        top.addWidget(self.cloud)
        self.device_label = QLabel()
        self.device_label.setObjectName("deviceSync")
        self.device_label.setWordWrap(True)
        top.addWidget(self.device_label, 1)
        card.addLayout(top)
        self.folder_label = QLabel()
        self.folder_label.setWordWrap(True)
        card.addWidget(self.folder_label)
        layout.addWidget(self.card)
        self.refresh()

    def refresh(self) -> None:
        self.logo.setPixmap(icons.app_icon().pixmap(30, 30))
        self.cloud.setPixmap(icons.cloud().pixmap(16, 16))

    def set_library(self, root: Path, *, shared: bool, status: str) -> None:
        self.device_label.setText(status if shared else "Not shared with other devices")
        self.cloud.setVisible(shared)
        self.folder_label.setText(f"Library folder: {_short_path(root)}")
        self.folder_label.setToolTip(str(root))


def _short_path(path: Path, parts: int = 2) -> str:
    tail = Path(path).parts[-parts:]
    return " › ".join(tail) if tail else str(path)
