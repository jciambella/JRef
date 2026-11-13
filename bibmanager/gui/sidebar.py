"""Left pane: navigation (special views + logical project folders, with counts) and the facet filter list."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from enum import StrEnum
from pathlib import Path
from typing import Any

from PySide6.QtCore import QMimeData, QModelIndex, QPersistentModelIndex, QRect, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QIcon, QPainter, QPalette, QStandardItem, QStandardItemModel
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QFrame,
    QListWidget,
    QListWidgetItem,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

from ..core.models import Collection, Paper
from . import icons, theme
from .paper_model import FACET_KINDS, PAPER_MIME, facet_values

KIND_ROLE = Qt.ItemDataRole.UserRole + 10
COLLECTION_ROLE = Qt.ItemDataRole.UserRole + 11
COUNT_ROLE = Qt.ItemDataRole.UserRole + 12


class NodeKind(StrEnum):
    ALL = "all"
    RECENT_ADDED = "recent_added"
    RECENT_READ = "recent_read"
    FAVORITES = "favorites"
    REVIEW = "review"
    MINE = "mine"
    UNSORTED = "unsorted"
    FOLDER = "folder"
    CREATE = "create"
    HEADER = "header"


SPECIAL_NODES: tuple[tuple[NodeKind, str], ...] = (
    (NodeKind.ALL, "All documents"),
    (NodeKind.RECENT_ADDED, "Recently added"),
    (NodeKind.RECENT_READ, "Recently read"),
    (NodeKind.FAVORITES, "Favorites"),
    (NodeKind.REVIEW, "Needs review"),
    (NodeKind.MINE, "My publications"),
    (NodeKind.UNSORTED, "Unsorted"),
)
NODE_TITLES = dict(SPECIAL_NODES)

# Where things may be dropped: documents onto folders / Favorites / My Publications,
# PDF files anywhere except the "Create Folder..." entry and the read-history view.
_PAPER_TARGETS = {NodeKind.FOLDER, NodeKind.FAVORITES, NodeKind.MINE}
_FILE_TARGETS = set(NodeKind) - {NodeKind.CREATE, NodeKind.RECENT_READ, NodeKind.HEADER}
_COUNTED = {NodeKind.ALL, NodeKind.FAVORITES, NodeKind.REVIEW, NodeKind.MINE, NodeKind.UNSORTED, NodeKind.FOLDER}

Node = tuple[NodeKind, int | None]
DropHandler = Callable[[QMimeData, NodeKind, int | None], bool]


def _special_icon(kind: NodeKind) -> QIcon:
    factories: dict[NodeKind, Callable[[], QIcon]] = {
        NodeKind.ALL: icons.all_documents,
        NodeKind.RECENT_ADDED: icons.recently_added,
        NodeKind.RECENT_READ: icons.recently_read,
        NodeKind.FAVORITES: icons.favorites,
        NodeKind.REVIEW: icons.needs_review,
        NodeKind.MINE: icons.person,
        NodeKind.UNSORTED: icons.unsorted,
    }
    return factories[kind]()


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
    return f"folder:{collection_id}" if kind == NodeKind.FOLDER else str(kind)


class NavDelegate(QStyledItemDelegate):
    """Fluent navigation item: rounded hover/selection, accent pill, icon, label and count."""

    ROW = 32
    HEADER = 34

    def sizeHint(self, option: QStyleOptionViewItem, index: QModelIndex | QPersistentModelIndex) -> QSize:
        height = self.HEADER if index.data(KIND_ROLE) == NodeKind.HEADER else self.ROW
        return QSize(super().sizeHint(option, index).width(), height)

    def updateEditorGeometry(self, editor: QWidget, option: QStyleOptionViewItem, index: Any) -> None:
        editor.setGeometry(option.rect.adjusted(32, 3, -6, -3))

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
            font.setPointSizeF(font.pointSizeF() * 0.92)
            painter.setFont(font)
            painter.setPen(theme.qcolor(t.text_secondary))
            painter.drawText(
                rect.adjusted(10, 10, -8, 0), int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), text
            )
            painter.restore()
            return
        bg = rect.adjusted(2, 2, -4, -2)
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)
        if selected or hovered:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(theme.qcolor(t.selected if selected else t.hover))
            painter.drawRoundedRect(bg, 5, 5)
        if selected:
            painter.setBrush(theme.qcolor(t.accent))
            painter.drawRoundedRect(QRectF(bg.left(), bg.center().y() - 8, 3, 16), 1.5, 1.5)
        icon = index.data(Qt.ItemDataRole.DecorationRole)
        if isinstance(icon, QIcon) and not icon.isNull():
            icon.paint(painter, QRect(int(bg.left()) + 10, int(bg.center().y()) - 8, 16, 16))
        text_rect = bg.adjusted(34, 0, -8, 0)
        count = index.data(COUNT_ROLE)
        if count:
            label = str(count)
            metrics = QFontMetrics(option.font)
            width = metrics.horizontalAdvance(label) + (12 if kind == NodeKind.REVIEW else 0)
            badge = QRectF(text_rect.right() - width, bg.center().y() - 9, width, 18)
            if kind == NodeKind.REVIEW:
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(theme.qcolor(t.warning_bg))
                painter.drawRoundedRect(badge, 9, 9)
                painter.setPen(theme.qcolor(t.warning))
            else:
                painter.setPen(theme.qcolor(t.text_tertiary))
            painter.setFont(option.font)
            painter.drawText(badge, int(Qt.AlignmentFlag.AlignCenter), label)
            text_rect.setRight(badge.left() - 6)
        painter.setFont(option.font)
        painter.setPen(theme.qcolor(t.text_secondary if kind == NodeKind.CREATE else t.text))
        elided = QFontMetrics(option.font).elidedText(text, Qt.TextElideMode.ElideRight, int(text_rect.width()))
        painter.drawText(text_rect, int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), elided)
        painter.restore()


class CollectionTree(QTreeView):
    nodeSelected = Signal(str, object)  # kind, collection id | None
    folderRenamed = Signal(int, str)
    createFolderRequested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("collections")
        self.source = CollectionModel(self)
        self.setModel(self.source)
        self.setHeaderHidden(True)
        self.setItemDelegate(NavDelegate(self))
        self.setIndentation(12)
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
        self._building = False
        self.selectionModel().currentChanged.connect(self._on_current_changed)
        self.source.itemChanged.connect(self._on_item_changed)
        self.clicked.connect(self._on_clicked)

    # ------------------------------------------------------------------ building
    def rebuild(self, collections: Iterable[Collection], select: Node | None = None) -> None:
        current = select or self.current_node()
        expanded = self._expanded_ids()
        self._building = True
        try:
            self.source.clear()
            root = self.source.invisibleRootItem()
            root.appendRow(self._header("Library"))
            for kind, title in SPECIAL_NODES:
                root.appendRow(self._item(title, kind, None, _special_icon(kind)))
            root.appendRow(self._header("Folders"))
            items: dict[int, QStandardItem] = {}
            pending = sorted(collections, key=lambda c: c.name.casefold())
            for collection in pending:
                item = self._item(collection.name, NodeKind.FOLDER, collection.id, icons.folder(), editable=True)
                item.setToolTip(f"{collection.name}\n.bib: {collection.slug}.bib")
                items[collection.id] = item
            for collection in pending:
                parent = items.get(collection.parent_id) if collection.parent_id else None
                (parent or root).appendRow(items[collection.id])
            root.appendRow(self._item("New folder", NodeKind.CREATE, None, icons.add_small()))
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

    def update_counts(self, counts: dict[str, int]) -> None:
        """Show document counts next to the views and folders (no rebuild, no signals)."""

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
                    kind != NodeKind.FOLDER or child.data(COLLECTION_ROLE) == collection_id
                ):
                    return child.index()
                found = walk(child)
                if found is not None:
                    return found
            return None

        return walk(self.source.invisibleRootItem()) or QModelIndex()

    def select_node(self, kind: NodeKind | str, collection_id: int | None = None) -> bool:
        index = self.find_index(NodeKind(kind), collection_id)
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


class FilterPane(QWidget):
    """ "Filter by Authors / Author Keywords / My Tags / Publications" list under the tree."""

    facetChanged = Signal(object, object)  # kind, value | None
    LABELS = {
        "authors": "Filter by Authors",
        "keywords": "Filter by Author Keywords",
        "tags": "Filter by My Tags",
        "publications": "Filter by Publications",
    }

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("facetPane")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.combo = QComboBox(self)
        self.combo.setObjectName("facetCombo")
        for kind in FACET_KINDS:
            self.combo.addItem(self.LABELS[kind], kind)
        self.list = QListWidget(self)
        self.list.setObjectName("facetList")
        self.list.setUniformItemSizes(True)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.combo)
        layout.addWidget(self.list)
        self._papers: list[Paper] = []
        self._populating = False
        self.combo.currentIndexChanged.connect(self._on_kind_changed)
        self.list.currentItemChanged.connect(self._on_item_changed)

    @property
    def kind(self) -> str:
        return str(self.combo.currentData())

    def selected_value(self) -> str | None:
        item = self.list.currentItem()
        return None if item is None or item.data(Qt.ItemDataRole.UserRole) is None else item.text()

    def set_papers(self, papers: Iterable[Paper]) -> None:
        self._papers = list(papers)
        self._populate(keep=self.selected_value())

    def _populate(self, keep: str | None = None) -> None:
        values = sorted({v for p in self._papers for v in facet_values(p, self.kind) if v}, key=str.casefold)
        self._populating = True
        try:
            self.list.clear()
            all_item = QListWidgetItem("All")
            self.list.addItem(all_item)
            selected = all_item
            for value in values:
                item = QListWidgetItem(value)
                item.setData(Qt.ItemDataRole.UserRole, value)
                self.list.addItem(item)
                if value == keep:
                    selected = item
            self.list.setCurrentItem(selected)
        finally:
            self._populating = False
        if keep is not None and selected is all_item:
            self.facetChanged.emit(self.kind, None)  # previous value vanished from this view

    def _on_kind_changed(self, _index: int) -> None:
        self._populate()
        self.facetChanged.emit(self.kind, None)

    def _on_item_changed(self, current: QListWidgetItem | None, _previous: QListWidgetItem | None) -> None:
        if not self._populating:
            self.facetChanged.emit(self.kind, self.selected_value())
