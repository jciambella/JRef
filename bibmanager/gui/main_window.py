"""Main window: toolbar, three panes, menus, shortcuts and every user action.

Layout (left to right): library tree + facet filter | document list | Details/Notes/Contents.
All mutations go through ``core.library.Library``; slow work goes through ``TaskRunner``.
"""

from __future__ import annotations

import dataclasses
from datetime import datetime
import logging
import os
import sys
from collections.abc import Callable, Iterable
from functools import partial
from pathlib import Path
from typing import Any

from PySide6.QtCore import (
    QItemSelection,
    QItemSelectionModel,
    QModelIndex,
    QObject,
    QPoint,
    QProcess,
    QSettings,
    QSize,
    Qt,
    QThreadPool,
    QTimer,
    QUrl,
    Signal,
)
from PySide6.QtGui import (
    QAction,
    QActionGroup,
    QCloseEvent,
    QDesktopServices,
    QDragEnterEvent,
    QDragMoveEvent,
    QDropEvent,
    QFontMetrics,
    QGuiApplication,
    QIcon,
    QKeySequence,
    QMouseEvent,
    QResizeEvent,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QTableView,
    QToolBar,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from .. import APP_NAME, APP_SUBTITLE
from ..core.config import user_config_path
from ..core.secrets import KeyStore
from ..core.extractor import pdf_outline
from ..core.inbox import InboxWatcher
from ..core.library import DuplicateDoiError, ForeignBibFileError, IngestResult, IngestStatus, Library, LibraryError
from ..core.mendeley import MendeleyImportPlan, MendeleyImportReport, default_mendeley_pdf_folder, summarize
from ..core.models import Attachment, Collection, Paper, split_list_field
from ..core.query import SCOPE_LABELS, SCOPES, SYNTAX_HELP, parse_query
from . import icons, theme
from .dialogs import (
    DeviceSyncDialog,
    FindPdfsDialog,
    MendeleyImportDialog,
    PreferencesDialog,
    ViewSettingsDialog,
    confirm_naming_rule,
    show_about,
    show_attach_report,
    show_import_report,
)
from .inspector import InspectorPanel
from .paper_model import (
    COLUMN_NAMES,
    DEFAULT_HIDDEN,
    DEFAULT_WIDTHS,
    ICON_COLUMNS,
    PAPER_ID_ROLE,
    Col,
    IconDelegate,
    PaperFilterProxy,
    PaperTableModel,
    TwoLineDelegate,
    paper_ids_from_mime,
)
from .sidebar import NODE_TITLES, CollectionTree, FilterPane, Node, NodeKind
from .workers import (
    AttachController,
    AttachReport,
    ImportController,
    MendeleyImportController,
    TaskRunner,
    expand_pdf_paths,
)


class InboxBridge(QObject):
    """Carries inbox events from the watcher thread to the GUI thread (queued signal delivery)."""

    fileReady = Signal(str)

    def emit_path(self, path: Path) -> None:  # called in the watcher thread
        self.fileReady.emit(str(path))


log = logging.getLogger(__name__)

SEARCH_METADATA = "metadata"
DEVICE_SYNC_INTERVAL_MS = 20_000  # shared library: how often the other devices' journals are checked
SEARCH_FULLTEXT = "fulltext"
RECENT_LIMIT = 100


# --------------------------------------------------------------------------- document table
class EmptyState(QWidget):
    """Centred illustration + call to action shown when the list is empty."""

    def __init__(self, parent: QWidget, on_add: Callable[[], object]) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, False)
        layout = QVBoxLayout(self)
        layout.addStretch(1)
        self.picture = QLabel(alignment=Qt.AlignmentFlag.AlignCenter)
        self.title = QLabel(alignment=Qt.AlignmentFlag.AlignCenter)
        self.title.setObjectName("emptyTitle")
        self.text = QLabel(alignment=Qt.AlignmentFlag.AlignCenter)
        self.text.setObjectName("emptyText")
        self.text.setWordWrap(True)
        self.button = QPushButton("Add documents")
        self.button.setObjectName("primary")
        self.button.clicked.connect(lambda: on_add())
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(self.button)
        row.addStretch(1)
        for widget in (self.picture, self.title, self.text):
            layout.addWidget(widget)
        layout.addSpacing(8)
        layout.addLayout(row)
        layout.addStretch(2)
        self.refresh()

    def refresh(self) -> None:
        self.picture.setPixmap(icons.empty_library().pixmap(72, 72))

    def show_for(self, *, filtered: bool) -> None:
        if filtered:
            self.title.setText("No matching documents")
            self.text.setText("Try another search term or clear the filter on the left.")
        else:
            self.title.setText("No documents here yet")
            self.text.setText("Drag and drop PDF files here, or add them from your computer.")
        self.button.setVisible(not filtered)
        self.show()


class DocumentTable(QTableView):
    """The centre list: drags documents out (to folders, or ``\\cite{}`` into an editor), accepts PDF files in."""

    def __init__(self, parent: QWidget | None = None, on_add: Callable[[], object] = lambda: None) -> None:
        super().__init__(parent)
        self.setObjectName("documents")
        self.files_dropped: Callable[[list[str], int], object] | None = None  # (paths, row under the cursor)
        self.hover_row = -1
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setSortingEnabled(True)
        self.setShowGrid(False)
        self.setWordWrap(False)
        self.setMouseTracking(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DragDrop)
        self.setDefaultDropAction(Qt.DropAction.CopyAction)
        self.setDropIndicatorShown(False)
        self.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.verticalHeader().hide()
        header = self.horizontalHeader()
        header.setSectionsMovable(True)
        header.setHighlightSections(False)
        header.setMinimumSectionSize(18)
        header.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        header.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.setItemDelegate(TwoLineDelegate(self))
        icon_delegate = IconDelegate(self)
        for col in ICON_COLUMNS:
            self.setItemDelegateForColumn(int(col), icon_delegate)
        self.empty_state = EmptyState(self.viewport(), on_add)
        self.empty_state.hide()

    def update_row_height(self) -> None:
        self.ensurePolished()
        metrics = QFontMetrics(self.font())
        self.verticalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
        self.verticalHeader().setDefaultSectionSize(2 * metrics.lineSpacing() + 18)
        self.horizontalHeader().setFixedHeight(metrics.height() + 16)

    def update_empty_state(self, *, empty: bool, filtered: bool) -> None:
        if empty:
            self.empty_state.setGeometry(self.viewport().rect())
            self.empty_state.show_for(filtered=filtered)
        else:
            self.empty_state.hide()

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self.empty_state.setGeometry(self.viewport().rect())

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        row = self.rowAt(int(event.position().y()))
        if row != self.hover_row:
            self.hover_row = row
            self.viewport().update()
        super().mouseMoveEvent(event)

    def leaveEvent(self, event: Any) -> None:
        self.hover_row = -1
        self.viewport().update()
        super().leaveEvent(event)

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()  # rows dragged inside the list are not "dropped" back onto it

    def dragMoveEvent(self, event: QDragMoveEvent) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
            row = self.rowAt(int(event.position().y()))  # a single PDF dropped on a row is attached to it
            if row != self.hover_row:
                self.hover_row = row
                self.viewport().update()
        else:
            event.ignore()

    def dropEvent(self, event: QDropEvent) -> None:
        paths = [u.toLocalFile() for u in event.mimeData().urls() if u.isLocalFile()]
        if paths and self.files_dropped is not None:
            event.acceptProposedAction()
            self.files_dropped(paths, self.rowAt(int(event.position().y())))
        else:
            event.ignore()


# --------------------------------------------------------------------------- main window
class MainWindow(QMainWindow):
    libraryChangeRequested = Signal(object)  # AppConfig of the library to open instead
    INBOX_SETTLE_S = 2.0  # a file must stay unchanged this long before it is imported

    def __init__(
        self,
        library: Library,
        *,
        settings: QSettings | None = None,
        max_threads: int = 3,
        user_config: Path | None = None,
        watch_inbox: bool | None = None,
    ) -> None:
        super().__init__()
        self.library = library
        self.settings = settings if settings is not None else QSettings()
        self.user_config = user_config if user_config is not None else user_config_path()
        self.theme_mode = str(self.settings.value("view/theme", "system"))
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(max_threads)
        self.runner = TaskRunner(self.pool, self)
        self.importer = ImportController(library, self.runner, self)
        self.mendeley = MendeleyImportController(library, self.runner, self)
        self.attacher = AttachController(library, self.runner, self)
        self._attach_search = False
        self.inbox_bridge = InboxBridge(self)
        self.inbox: InboxWatcher | None = None
        self._watch_inbox = watch_inbox
        self.node: Node = (NodeKind.ALL, None)
        self.search_mode = SEARCH_METADATA
        self.search_scope = "all"
        self._outline_for: int | None = None
        self._reload_timer = QTimer(self)
        self._reload_timer.setSingleShot(True)
        self._reload_timer.setInterval(150)
        self._reload_timer.timeout.connect(lambda: self.reload_view(keep_selection=True))
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(200)
        self._search_timer.timeout.connect(self._apply_search)
        # Shared library (core/sync.py): journal local changes and apply the other devices' changes regularly.
        self._device_timer = QTimer(self)
        self._device_timer.setInterval(DEVICE_SYNC_INTERVAL_MS)
        self._device_timer.timeout.connect(self._device_sync_tick)

        self.setWindowTitle(f"{APP_NAME} \u2014 {APP_SUBTITLE}")
        self.setWindowIcon(icons.app_icon())
        self.resize(1480, 900)
        self._create_actions()
        self._create_menus()
        self._create_toolbar()
        self._create_panes()
        self._create_statusbar()
        self._wire()
        self._restore_state()
        self.apply_theme(self.theme_mode, rebuild=False)
        self.reload_tree(select=self._saved_node())
        hints = QGuiApplication.styleHints()
        if hasattr(hints, "colorSchemeChanged"):
            hints.colorSchemeChanged.connect(self._on_system_scheme_changed)
        self.restart_inbox()

    # ================================================================== construction
    def _action(
        self,
        text: str,
        slot: Callable[..., Any],
        *,
        shortcut: str | QKeySequence.StandardKey | None = None,
        icon: QIcon | None = None,
        tip: str | None = None,
        checkable: bool = False,
        context: Qt.ShortcutContext = Qt.ShortcutContext.WindowShortcut,
    ) -> QAction:
        action = QAction(text, self)
        if icon is not None:
            action.setIcon(icon)
        if shortcut is not None:
            action.setShortcut(QKeySequence(shortcut))
            action.setShortcutContext(context)
        if tip:
            action.setToolTip(tip)
            action.setStatusTip(tip)
        action.setCheckable(checkable)
        if checkable:
            action.toggled.connect(slot)
        else:
            action.triggered.connect(lambda _checked=False: slot())
        self.addAction(action)
        return action

    def _create_actions(self) -> None:
        a = self._action
        table_only = Qt.ShortcutContext.WidgetWithChildrenShortcut
        self.act_add_files = a("Add Files...", self.add_files_dialog, shortcut="Ctrl+O",
                               tip="Add PDF files to the library (Ctrl+O)")  # fmt: skip
        self.act_add_primary = QAction("Add", self)
        self.act_add_primary.setToolTip("Add PDF files to the library (Ctrl+O)")
        self.act_add_primary.triggered.connect(lambda _checked=False: self.add_files_dialog())
        self.act_add_folder = a("Add Folder...", self.add_folder_dialog, tip="Import every PDF in a folder")
        self.act_new_folder = a("New Folder...", self.new_folder, shortcut="Ctrl+Shift+N",
                                tip="Create a project folder (with its own .bib file)")  # fmt: skip
        self.act_new_folder.setIconText("New folder")
        self.act_remove_folder = a("Remove Folder...", self.remove_folder,
                                   tip="Remove the selected folder (documents stay in the library)")  # fmt: skip
        self.act_remove_folder.setIconText("Remove folder")
        self.act_sync = a("Synchronise .bib Files", self.sync_bibs, shortcut="F5",
                          tip="Rewrite every folder .bib file and master.bib (F5)")  # fmt: skip
        self.act_sync.setIconText("Sync")
        self.act_help = a("Shortcuts && About", self.show_help, shortcut="F1", tip="Help: shortcuts and about")
        self.act_preferences = a("Preferences...", self.open_preferences, shortcut="Ctrl+,",
                                 tip="Preferences: library folder, theme, contact e-mail")  # fmt: skip
        self.act_quit = a("Quit", self.close, shortcut=QKeySequence.StandardKey.Quit)
        self.act_open = a("Open File", self.open_selected, shortcut="Return", context=table_only)
        self.act_show_in_folder = a("Show in Folder", self.show_selected_in_folder)
        self.act_copy_cite = a("Copy Citation (\\cite{...})", self.copy_citation, shortcut="Ctrl+Shift+C")
        self.act_copy_bibtex = a("Copy BibTeX Entry", self.copy_bibtex, shortcut="Ctrl+Shift+B")
        self.act_delete = a("Delete Documents...", self.delete_selected, shortcut=QKeySequence.StandardKey.Delete,
                            context=table_only)  # fmt: skip
        self.act_select_all = a("Select All", lambda: self.table.selectAll())
        self.act_find = a("Search", self.focus_search, shortcut=QKeySequence.StandardKey.Find)
        self.act_lookup = a("Update Details Online", lambda: self.lookup_selected("auto"),
                            tip="Fetch metadata from Crossref / arXiv / OpenAlex")  # fmt: skip
        self.act_show_filter = a("Filter Pane", self._set_filter_visible, checkable=True)
        self.act_show_details = a("Details Pane", self._set_details_visible, checkable=True)
        self.act_open_library = a(
            "Open Library Folder", lambda: self._launch(QUrl.fromLocalFile(str(self.library.root)))
        )
        self.act_open_bib_dir = a(
            "Open BibTeX Folder", lambda: self._launch(QUrl.fromLocalFile(str(self.library.bib.bibtex_dir)))
        )
        self.act_edit_settings = a("Edit Settings...", lambda: self.edit_view_settings())
        self.act_import_mendeley = a("Import Mendeley Library...", self.import_mendeley_dialog,
                                     tip="Build the library from Mendeley's BibTeX export")  # fmt: skip
        self.act_apply_naming = a("Apply Naming Rule to Existing Keys...", self.apply_naming_rule,
                                  tip="Re-key documents as Surname_Year, Surname1_Surname2_Year, ...")  # fmt: skip
        self.act_attach_pdf = a("Attach PDF...", self.attach_pdf_dialog,
                                tip="Choose the PDF of the selected reference, or drop a PDF onto its row")  # fmt: skip
        self.act_add_supplements = a("Add Supplementary Files...", self.add_supplements_dialog,
                                     tip="Attach supplementary information, data or videos to the selected document")  # fmt: skip
        self.act_find_pdfs = a("Find Missing PDFs...", self.find_missing_pdfs,
                               tip="Search a folder for the PDFs of documents that have none")  # fmt: skip
        self.act_open_inbox = a("Open Inbox Folder", self.open_inbox_folder,
                                tip="PDFs saved here are imported automatically")  # fmt: skip
        self.act_share = a("Share Library with Other Devices...", self.device_sync_dialog,
                           tip="Keep this library identical on another PC or the Android app (synced folder)")  # fmt: skip
        self.act_sync_devices = a("Synchronise Devices Now", lambda: self.sync_devices(manual=True), shortcut="F6",
                                  tip="Send this device's changes and apply those of the other devices (F6)")  # fmt: skip

    def _create_menus(self) -> None:
        bar = self.menuBar()
        file_menu = bar.addMenu("&File")
        for item in (self.act_add_files, self.act_add_folder, self.act_import_mendeley, None, self.act_new_folder,
                     None, self.act_open, self.act_show_in_folder, None, self.act_quit):  # fmt: skip
            file_menu.addSeparator() if item is None else file_menu.addAction(item)
        edit_menu = bar.addMenu("&Edit")
        for item in (self.act_copy_cite, self.act_copy_bibtex, None, self.act_select_all, self.act_find, None,
                     self.act_delete):  # fmt: skip
            edit_menu.addSeparator() if item is None else edit_menu.addAction(item)
        view_menu = bar.addMenu("&View")
        view_menu.addAction(self.act_show_filter)
        view_menu.addAction(self.act_show_details)
        view_menu.addSeparator()
        self.columns_menu = view_menu.addMenu("Columns")
        self.columns_menu.aboutToShow.connect(lambda: self._fill_columns_menu(self.columns_menu))
        view_menu.addAction(self.act_edit_settings)
        tools_menu = bar.addMenu("&Tools")
        for item in (self.act_sync, self.act_lookup, self.act_apply_naming, self.act_find_pdfs, None,
                     self.act_share, self.act_sync_devices, None,
                     self.act_open_library, self.act_open_bib_dir, self.act_open_inbox, None,
                     self.act_preferences):  # fmt: skip
            tools_menu.addSeparator() if item is None else tools_menu.addAction(item)
        help_menu = bar.addMenu("&Help")
        help_menu.addAction(self.act_help)

    def _command_button(
        self, action: QAction, *, primary: bool = False, menu: QMenu | None = None, text: bool = True
    ) -> QToolButton:
        button = QToolButton()
        button.setObjectName("primaryCommand" if primary else "command")
        button.setDefaultAction(action)
        button.setToolButtonStyle(
            Qt.ToolButtonStyle.ToolButtonTextBesideIcon if text else Qt.ToolButtonStyle.ToolButtonIconOnly
        )
        button.setIconSize(QSize(18, 18))
        button.setAutoRaise(True)
        if menu is not None:
            button.setMenu(menu)
            button.setPopupMode(QToolButton.ToolButtonPopupMode.MenuButtonPopup)
        return button

    @staticmethod
    def _command_separator() -> QFrame:
        line = QFrame()
        line.setObjectName("commandSeparator")
        line.setFrameShape(QFrame.Shape.VLine)
        line.setFixedWidth(1)
        return line

    def _create_toolbar(self) -> None:
        bar = QToolBar("Commands", self)
        bar.setObjectName("commandBar")
        bar.setMovable(False)
        bar.setFloatable(False)
        bar.setContextMenuPolicy(Qt.ContextMenuPolicy.PreventContextMenu)
        self.addToolBar(Qt.ToolBarArea.TopToolBarArea, bar)
        add_menu = QMenu(self)
        add_menu.addAction(self.act_add_files)
        add_menu.addAction(self.act_add_folder)
        add_menu.addSeparator()
        add_menu.addAction(self.act_import_mendeley)
        bar.addWidget(self._command_button(self.act_add_primary, primary=True, menu=add_menu))
        bar.addWidget(self._command_button(self.act_new_folder))
        bar.addWidget(self._command_button(self.act_remove_folder))
        bar.addWidget(self._command_separator())
        bar.addWidget(self._command_button(self.act_sync))
        self.command_labels = ["Add", "New folder", "Remove folder", "Sync"]
        for _ in range(2):
            spacer = QWidget()
            spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
            bar.addWidget(spacer)
            if _ == 0:
                self.search = QLineEdit()
                self.search.setObjectName("searchBox")
                self.search.setMinimumWidth(320)
                self.search.setMaximumWidth(460)
                self.search.setClearButtonEnabled(True)
                bar.addWidget(self.search)
        self.search_menu = QMenu(self)
        group = QActionGroup(self)
        header = self.search_menu.addAction("Search in:")
        header.setEnabled(False)
        self.scope_actions: dict[str, QAction] = {}
        for scope in SCOPES:
            action = QAction(SCOPE_LABELS[scope], self, checkable=True)
            group.addAction(action)
            self.search_menu.addAction(action)
            action.triggered.connect(lambda _=False, sc=scope: self.set_search_scope(sc))
            self.scope_actions[scope] = action
        self.act_search_meta = self.scope_actions["all"]
        self.search_menu.addSeparator()
        self.act_search_full = QAction("Full text (PDF contents)", self, checkable=True)
        group.addAction(self.act_search_full)
        self.search_menu.addAction(self.act_search_full)
        self.act_search_full.triggered.connect(lambda _=False: self.set_search_mode(SEARCH_FULLTEXT))
        self.search_menu.addSeparator()
        self.search_menu.addAction("Search syntax...").triggered.connect(lambda _=False: self.show_search_help())
        self.search_mode_action = self.search.addAction(icons.search(), QLineEdit.ActionPosition.LeadingPosition)
        self.search_mode_action.setToolTip("Choose what to search")
        self.search_mode_action.triggered.connect(
            lambda: self.search_menu.exec(self.search.mapToGlobal(QPoint(0, self.search.height())))
        )
        bar.addWidget(self._command_button(self.act_help, text=False))
        bar.addWidget(self._command_button(self.act_preferences, text=False))

    @staticmethod
    def _card(*widgets: QWidget, margins: tuple[int, int, int, int] = (0, 0, 0, 0)) -> QFrame:
        card = QFrame()
        card.setObjectName("card")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(*margins)
        layout.setSpacing(0)
        for widget in widgets:
            layout.addWidget(widget)
        return card

    def _create_panes(self) -> None:
        self.tree = CollectionTree()
        self.tree.source.drop_handler = self._on_tree_drop
        self.filter_pane = FilterPane()
        self.left_split = QSplitter(Qt.Orientation.Vertical)
        self.left_split.addWidget(self.tree)
        self.left_split.addWidget(self.filter_pane)
        self.left_split.setStretchFactor(0, 3)
        self.left_split.setStretchFactor(1, 2)

        header = QWidget()
        header.setObjectName("listHeader")
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(18, 12, 12, 8)
        titles = QVBoxLayout()
        titles.setSpacing(0)
        self.list_title = QLabel()
        self.list_title.setObjectName("listTitle")
        self.count_label = QLabel()
        self.count_label.setObjectName("listSubtitle")
        titles.addWidget(self.list_title)
        titles.addWidget(self.count_label)
        header_layout.addLayout(titles)
        header_layout.addStretch(1)
        self.edit_settings = QPushButton("Edit settings")
        self.edit_settings.setObjectName("subtle")
        self.edit_settings.setToolTip("Columns and, for a folder, its external .bib file")
        self.edit_settings.clicked.connect(lambda: self.edit_view_settings())
        header_layout.addWidget(self.edit_settings, 0, Qt.AlignmentFlag.AlignVCenter)

        self.model = PaperTableModel(self)
        self.proxy = PaperFilterProxy(self)
        self.proxy.setSourceModel(self.model)
        self.table = DocumentTable(on_add=self.add_files_dialog)
        self.table.setModel(self.proxy)
        self.table.files_dropped = self._on_files_dropped
        self.table.addAction(self.act_open)
        self.table.addAction(self.act_delete)
        self.table.update_row_height()

        self.inspector = InspectorPanel()
        self.main_split = QSplitter(Qt.Orientation.Horizontal)
        self.main_split.addWidget(self._card(self.left_split, margins=(4, 6, 4, 4)))
        self.main_split.addWidget(self._card(header, self.table, margins=(1, 1, 1, 6)))
        self.main_split.addWidget(self._card(self.inspector, margins=(6, 2, 4, 6)))
        self.main_split.setStretchFactor(1, 1)
        self.main_split.setCollapsible(1, False)
        self.main_split.setSizes([270, 870, 400])
        central = QWidget()
        central.setObjectName("central")
        outer = QVBoxLayout(central)
        outer.setContentsMargins(10, 0, 10, 0)
        outer.addWidget(self.main_split)
        self.setCentralWidget(central)
        self._apply_default_columns()

    def _create_statusbar(self) -> None:
        bar = self.statusBar()
        bar.setSizeGripEnabled(True)
        self.filter_toggle = QToolButton()
        self.filter_toggle.setObjectName("statusButton")
        self.filter_toggle.setIconSize(QSize(14, 14))
        self.filter_toggle.setCheckable(True)
        self.filter_toggle.setToolTip("Show or hide the filter pane")
        self.filter_toggle.toggled.connect(self.act_show_filter.setChecked)
        bar.addWidget(self.filter_toggle)
        self.status_label = QLabel()
        bar.addWidget(self.status_label, 1)
        self.progress = QProgressBar()
        self.progress.setFixedWidth(160)
        self.progress.setTextVisible(False)
        self.progress.hide()
        bar.addPermanentWidget(self.progress)
        self.device_label = QLabel()
        self.device_label.setObjectName("deviceSync")
        self.device_label.setToolTip("Library shared with other devices (Tools > Share Library with Other Devices)")
        bar.addPermanentWidget(self.device_label)
        self._update_device_label()
        if self.library.sync is not None:
            self._device_timer.start()
            QTimer.singleShot(1500, self._device_sync_tick)

    def _wire(self) -> None:
        self.tree.nodeSelected.connect(self._on_node_selected)
        self.tree.folderRenamed.connect(self._rename_folder)
        self.tree.createFolderRequested.connect(self.new_folder)
        self.tree.customContextMenuRequested.connect(self._tree_menu)
        self.filter_pane.facetChanged.connect(self._on_facet_changed)
        self.table.selectionModel().selectionChanged.connect(self._on_selection_changed)
        self.table.doubleClicked.connect(lambda _index: self.open_selected())
        self.table.clicked.connect(self._on_table_clicked)
        self.table.customContextMenuRequested.connect(self._table_menu)
        self.table.horizontalHeader().customContextMenuRequested.connect(self._header_menu)
        self.inspector.fieldEdited.connect(self._on_field_edited)
        self.inspector.notesEdited.connect(self._on_notes_edited)
        self.inspector.lookupRequested.connect(self.lookup_selected)
        self.inspector.openFileRequested.connect(self.open_selected)
        self.inspector.openUrlRequested.connect(self._open_url)
        self.inspector.revealFileRequested.connect(self.show_selected_in_folder)
        self.inspector.markReviewedRequested.connect(self._mark_reviewed)
        self.search.textChanged.connect(lambda _text: self._search_timer.start())
        self.importer.progress.connect(self._on_import_progress)
        self.importer.imported.connect(self._on_imported)
        self.importer.failed.connect(self._on_import_failed)
        self.importer.batchFinished.connect(self._on_import_finished)
        self.mendeley.planned.connect(self._on_mendeley_planned)
        self.mendeley.progress.connect(self._on_import_progress)
        self.mendeley.finished.connect(self._on_mendeley_finished)
        self.inbox_bridge.fileReady.connect(self._on_inbox_file)
        self.inspector.attachFileRequested.connect(self.attach_pdf_dialog)
        self.inspector.attachmentAction.connect(self._on_attachment_action)
        self.inspector.addSupplementsRequested.connect(self.add_supplements_dialog)
        self.inspector.assistant.bind(
            self.library,
            KeyStore(self.user_config.parent),
            save_settings=self._save_assistant_settings,
            launch=self._launch,
            reveal=self._reveal,
        )
        self.inspector.assistant.paperChanged.connect(self._on_assistant_notes)
        self.attacher.progress.connect(self._on_import_progress)
        self.attacher.attached.connect(self._on_pdf_attached)
        self.attacher.finished.connect(self._on_attach_finished)
        for signal in (
            self.proxy.modelReset,
            self.proxy.rowsInserted,
            self.proxy.rowsRemoved,
            self.proxy.layoutChanged,
        ):
            signal.connect(self._update_count)

    # ================================================================== persistence
    def _apply_default_columns(self) -> None:
        header = self.table.horizontalHeader()
        for col in Col:
            header.resizeSection(int(col), DEFAULT_WIDTHS[col])
            header.setSectionHidden(int(col), col in DEFAULT_HIDDEN)
        self._fix_resize_modes()
        self.table.sortByColumn(int(Col.ADDED), Qt.SortOrder.DescendingOrder)

    def _fix_resize_modes(self) -> None:
        header = self.table.horizontalHeader()
        for col in ICON_COLUMNS:
            header.setSectionResizeMode(int(col), QHeaderView.ResizeMode.Fixed)
            header.resizeSection(int(col), DEFAULT_WIDTHS[col])
        header.setSectionResizeMode(int(Col.TITLE), QHeaderView.ResizeMode.Stretch)

    def _restore_state(self) -> None:
        s = self.settings
        if (geometry := s.value("window/geometry")) is not None:
            self.restoreGeometry(geometry)
        if (state := s.value("window/mainSplitter")) is not None:
            self.main_split.restoreState(state)
        if (state := s.value("window/leftSplitter")) is not None:
            self.left_split.restoreState(state)
        if (state := s.value("table/header")) is not None and self.table.horizontalHeader().restoreState(state):
            self._fix_resize_modes()
        scope = str(s.value("search/scope", "all"))
        self.search_scope = scope if scope in SCOPES else "all"
        self.set_search_mode(str(s.value("search/mode", SEARCH_METADATA)))
        self.act_show_filter.setChecked(bool(s.value("view/filterPane", True, type=bool)))
        self.act_show_details.setChecked(bool(s.value("view/detailsPane", True, type=bool)))
        self._set_filter_visible(self.act_show_filter.isChecked())

    def _saved_node(self) -> Node:
        text = str(self.settings.value("view/node", "all"))
        kind, _, cid = text.partition(":")
        try:
            return NodeKind(kind), int(cid) if cid else None
        except ValueError:
            return NodeKind.ALL, None

    def _save_state(self) -> None:
        s = self.settings
        s.setValue("window/geometry", self.saveGeometry())
        s.setValue("window/mainSplitter", self.main_split.saveState())
        s.setValue("window/leftSplitter", self.left_split.saveState())
        s.setValue("table/header", self.table.horizontalHeader().saveState())
        s.setValue("search/mode", self.search_mode)
        s.setValue("search/scope", self.search_scope)
        s.setValue("view/filterPane", self.act_show_filter.isChecked())
        s.setValue("view/detailsPane", self.act_show_details.isChecked())
        kind, cid = self.node
        s.setValue("view/node", f"{kind}:{cid}" if cid is not None else str(kind))
        s.sync()

    def closeEvent(self, event: QCloseEvent) -> None:
        focus = QApplication.focusWidget()
        if focus is not None:
            focus.clearFocus()  # commits an inline edit in progress
        self.inspector.flush_notes()
        if self.mendeley.running:
            answer = QMessageBox.question(
                self, "Import in progress", "The Mendeley import is still running. Quit anyway?"
            )
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            self.mendeley.cancel()
        if self.importer.pending:
            answer = QMessageBox.question(
                self, "Imports in progress", f"{self.importer.pending} import(s) are still running. Quit anyway?"
            )
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            self.pool.clear()
        self.stop_inbox()
        self._device_timer.stop()
        try:
            self.library.push_changes()  # the last edits reach the journal before the window goes
        except Exception:  # the library may already be closed (it was moved to another folder)
            pass
        self.pool.waitForDone(3000)
        if self.mendeley.running:  # deliver the cancelled tasks so the deferred .bib writes are flushed
            self.runner.wait_idle(10.0)
        self._save_state()
        event.accept()

    # ================================================================== views
    def reload_tree(self, select: Node | None = None) -> None:
        self.tree.rebuild(self.library.collections(), select=select)
        self.node = self.tree.current_node() or (NodeKind.ALL, None)
        self.reload_view(keep_selection=False)

    def _papers_for(self, node: Node) -> list[Paper]:
        kind, cid = node
        lib = self.library
        if kind == NodeKind.FOLDER and cid is not None:
            return lib.papers(cid)
        loaders: dict[NodeKind, Callable[[], list[Paper]]] = {
            NodeKind.ALL: lib.papers,
            NodeKind.RECENT_ADDED: lambda: lib.recently_added(RECENT_LIMIT),
            NodeKind.RECENT_READ: lambda: lib.recently_read(RECENT_LIMIT),
            NodeKind.FAVORITES: lib.favorite_papers,
            NodeKind.REVIEW: lib.papers_needing_review,
            NodeKind.MINE: lib.my_publications,
            NodeKind.UNSORTED: lib.unassigned_papers,
        }
        return loaders.get(kind, lib.papers)()

    def reload_view(self, keep_selection: bool = True) -> None:
        selected = self.selected_ids() if keep_selection else []
        papers = self._papers_for(self.node)
        self.model.set_papers(papers, self.library.folder_names_by_paper())
        self.filter_pane.set_papers(papers)
        self._apply_search()
        self._update_header()
        if selected:
            self.select_ids(selected)
        else:
            self.inspector.clear()
        self._update_count()
        self._refresh_counts()

    def _on_node_selected(self, kind: str, collection_id: int | None) -> None:
        self.node = (NodeKind(kind), collection_id)
        self.reload_view(keep_selection=False)
        self.table.scrollToTop()

    def _update_header(self) -> None:
        kind, cid = self.node
        if kind == NodeKind.FOLDER and cid is not None:
            title = self.tree.collection_name(cid)
        else:
            title = NODE_TITLES.get(kind, "All documents")
        self.list_title.setText(title)

    def _update_count(self, *_args: Any) -> None:
        shown, total = self.proxy.rowCount(), self.model.rowCount()
        noun = "document" if total == 1 else "documents"
        self.count_label.setText(f"{shown} of {total} {noun}" if shown != total else f"{total} {noun}")
        self.table.update_empty_state(empty=shown == 0, filtered=total > 0)

    def _refresh_counts(self) -> None:
        self.tree.update_counts(self.library.counts())

    def set_search_mode(self, mode: str) -> None:
        self.search_mode = mode if mode in (SEARCH_METADATA, SEARCH_FULLTEXT) else SEARCH_METADATA
        fulltext = self.search_mode == SEARCH_FULLTEXT
        self.act_search_full.setChecked(fulltext)
        self.scope_actions[self.search_scope].setChecked(not fulltext)
        if fulltext:
            self.search.setPlaceholderText("Search full text...  (author:, journal:, year: also work)")
        elif self.search_scope == "all":
            self.search.setPlaceholderText("Search...  (e.g. author:anand journal:solids year:2020)")
        else:
            self.search.setPlaceholderText(f"Search {SCOPE_LABELS[self.search_scope].lower()}...")
        what = "full text" if fulltext else SCOPE_LABELS[self.search_scope]
        self.search_mode_action.setToolTip(f"Searching: {what}. Click to choose what to search.")
        self.search.setToolTip(
            "Words are searched in the field chosen with the magnifier; qualifiers work everywhere:\n"
            + "\n".join(f"  {example}   {meaning}" for example, meaning in SYNTAX_HELP)
        )
        self._apply_search()

    def set_search_scope(self, scope: str) -> None:
        """Field searched by plain words (Mendeley's "Search in"); qualifiers such as author: always work."""
        self.search_scope = scope if scope in SCOPES else "all"
        self.set_search_mode(SEARCH_METADATA)

    def show_search_help(self) -> None:
        rows = "".join(f"<tr><td><code>{e}</code></td><td>&nbsp;&nbsp;{m}</td></tr>" for e, m in SYNTAX_HELP)
        box = QMessageBox(self)
        box.setWindowTitle("Search syntax")
        box.setTextFormat(Qt.TextFormat.RichText)
        box.setText(
            "<p>Words are searched in the field chosen from the magnifier menu (all fields by default). "
            "Terms are combined with AND; case and accents are ignored; use quotes for phrases.</p>"
            f"<table>{rows}</table>"
        )
        box.exec()

    def focus_search(self) -> None:
        self.search.setFocus()
        self.search.selectAll()

    def _apply_search(self) -> None:
        query = parse_query(self.search.text().strip())
        if self.search_mode == SEARCH_FULLTEXT and query.free_text:
            hits = self.library.search_fulltext(query.free_text, limit=5000)
            self.proxy.set_ids({hit.paper_id for hit in hits})
            self.proxy.set_query(query, "all", skip_free_text=True)  # qualifiers still filter the hits
        else:
            self.proxy.set_ids(None)
            self.proxy.set_query(query, self.search_scope)
        self._update_count()

    def _on_facet_changed(self, kind: str | None, value: str | None) -> None:
        self.proxy.set_facet(kind, value)
        self._update_count()

    # ================================================================== selection
    def selected_ids(self) -> list[int]:
        model = self.table.selectionModel()
        if model is None:
            return []
        rows = sorted(model.selectedRows(), key=lambda index: index.row())
        return [int(index.data(PAPER_ID_ROLE)) for index in rows]

    def selected_papers(self) -> list[Paper]:
        papers = []
        for pid in self.selected_ids():
            row = self.model.row_of(pid)
            if row is not None:
                papers.append(self.model.paper(row))
        return papers

    def select_ids(self, ids: Iterable[int]) -> None:
        selection = QItemSelection()
        first: QModelIndex | None = None
        for pid in ids:
            row = self.model.row_of(pid)
            if row is None:
                continue
            index = self.proxy.mapFromSource(self.model.index(row, int(Col.TITLE)))
            if not index.isValid():
                continue
            selection.select(index, index)
            first = first or index
        flags = QItemSelectionModel.SelectionFlag.ClearAndSelect | QItemSelectionModel.SelectionFlag.Rows
        self.table.selectionModel().select(selection, flags)
        if first is not None:
            self.table.selectionModel().setCurrentIndex(first, QItemSelectionModel.SelectionFlag.NoUpdate)
            self.table.scrollTo(first)
        else:
            self.inspector.clear()

    def _on_selection_changed(self, *_args: Any) -> None:
        papers = self.selected_papers()
        if len(papers) == 1:
            self._show_paper(papers[0])
        elif not papers:
            self.inspector.clear()
        else:
            self.inspector.clear(f"{len(papers)} documents selected")

    def _show_paper(self, paper: Paper) -> None:
        pdf = self.library.pdf_path(paper)
        name = pdf.name if pdf is not None else ""
        attachments = self.library.attachments(paper.id) if paper.attachments else []
        available = {a.id: self.library.attachment_available(a) for a in attachments}
        self.inspector.set_paper(
            paper, [c.name for c in self.library.collections_of(paper.id)], name, attachments, available
        )
        if pdf is None:
            self._outline_for = paper.id
            self.inspector.set_outline(paper.id, "No PDF attached", None, "This reference has no PDF.")
            return
        if self._outline_for != paper.id:
            self._outline_for = paper.id
            pid = paper.id
            self.runner.submit(
                pdf_outline,
                pdf,
                on_done=lambda outline: self.inspector.set_outline(pid, name, outline),
                on_error=lambda message: self.inspector.set_outline(pid, name, None, message),
            )

    def _refresh_paper(self, paper: Paper) -> None:
        self.model.replace_paper(paper)
        if self.inspector.paper_id == paper.id:
            self._show_paper(paper)

    # ================================================================== document actions
    def _launch(self, url: QUrl) -> bool:
        """Open a file/URL with the desktop default application (patched in tests)."""
        return QDesktopServices.openUrl(url)

    def open_selected(self) -> None:
        for paper in self.selected_papers()[:10]:
            path = self.library.pdf_path(paper)
            if path is None:
                if paper.meta.url or paper.meta.doi:
                    self._open_url(paper.meta.url or f"https://doi.org/{paper.meta.doi}")
                else:
                    self.notify(f"{paper.citation_key} has no PDF")
                continue
            if not path.exists():
                where = "at its linked location" if paper.pdf_linked else "from the library"
                QMessageBox.warning(self, "File not found", f"The PDF is missing {where}:\n{path}")
                continue
            self._launch(QUrl.fromLocalFile(str(path)))
            self._refresh_paper(self.library.mark_opened(paper.id))

    def show_selected_in_folder(self) -> None:
        papers = self.selected_papers()
        path = self.library.pdf_path(papers[0]) if papers else None
        if path is not None:
            self._reveal(path)

    def _reveal(self, path: Path) -> None:
        if sys.platform == "win32":
            QProcess.startDetached("explorer.exe", ["/select,", os.path.normpath(str(path))])
        elif sys.platform == "darwin":
            QProcess.startDetached("open", ["-R", str(path)])
        else:
            self._launch(QUrl.fromLocalFile(str(path.parent)))

    def _open_url(self, url: str) -> None:
        if url:
            self._launch(QUrl(url if "://" in url else f"https://{url}"))

    def copy_citation(self) -> None:
        papers = self.selected_papers()
        if not papers:
            return
        text = "\\cite{" + ",".join(p.citation_key for p in papers) + "}"
        QGuiApplication.clipboard().setText(text)
        self.notify(f"Copied {text}")

    def copy_bibtex(self) -> None:
        papers = self.selected_papers()
        if papers:
            QGuiApplication.clipboard().setText("\n\n".join(self.library.bibtex_entry(p) for p in papers) + "\n")
            self.notify(f"Copied {len(papers)} BibTeX entr{'y' if len(papers) == 1 else 'ies'}")

    def _apply_flags(self, ids: Iterable[int], **flags: Any) -> None:
        for pid in ids:
            self._refresh_paper(self.library.update_flags(pid, **flags))
        self._refresh_counts()

    def _on_table_clicked(self, index: QModelIndex) -> None:
        if not index.isValid():
            return
        pid = int(index.data(PAPER_ID_ROLE))
        row = self.model.row_of(pid)
        if row is None:
            return
        paper = self.model.paper(row)
        if index.column() == Col.FAVORITE:
            self._apply_flags([pid], favorite=not paper.favorite)
        elif index.column() == Col.READ:
            self._apply_flags([pid], is_read=not paper.is_read)

    def delete_selected(self) -> None:
        papers = self.selected_papers()
        if not papers:
            return
        keys = ", ".join(p.citation_key for p in papers[:6]) + (", ..." if len(papers) > 6 else "")
        linked = sum(p.pdf_linked for p in papers)
        files = "The PDF files in the library are deleted from disk"
        if linked:
            files += f"; {linked} linked PDF(s) outside it are left where they are"
        answer = QMessageBox.question(
            self,
            "Delete Documents",
            f"Delete {len(papers)} document(s) from the library?\n\n{keys}\n\n"
            f"{files}, and the entries are removed from every folder and .bib file.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        for paper in papers:
            self.library.delete_paper(paper.id)
        self.reload_view(keep_selection=False)
        self.notify(f"Deleted {len(papers)} document(s)")

    def add_to_folder(self, ids: Iterable[int], collection_id: int) -> None:
        added = sum(self.library.add_to_collection(pid, collection_id) for pid in ids)
        self.notify(f"Added {added} document(s) to '{self.tree.collection_name(collection_id)}'")
        self._schedule_reload()

    def remove_from_current_folder(self) -> None:
        kind, cid = self.node
        if kind != NodeKind.FOLDER or cid is None:
            return
        papers = self.selected_papers()
        for paper in papers:
            self.library.remove_from_collection(paper.id, cid)
        self.reload_view(keep_selection=False)
        self.notify(f"Removed {len(papers)} document(s) from '{self.tree.collection_name(cid)}' (PDFs kept)")

    # ================================================================== inspector edits
    def _on_field_edited(self, field: str, value: Any) -> None:
        pid = self.inspector.paper_id
        if pid is None:
            return
        before = self.library.get_paper(pid)
        try:
            if field == "tags":
                paper = self.library.update_flags(pid, tags=str(value))
            else:
                paper = self.library.update_metadata(pid, **{field: value})
        except (ValueError, TypeError, LibraryError, KeyError, OSError) as exc:
            QMessageBox.warning(self, "Cannot update document", str(exc))
            paper = self.library.get_paper(pid)
        self._refresh_paper(paper)
        if paper.pdf_managed and before.pdf_managed and paper.relative_pdf_path != before.relative_pdf_path:
            self.notify(f"PDF renamed: {before.relative_pdf_path} → {paper.relative_pdf_path}")

    def _on_notes_edited(self, paper_id: int, text: str) -> None:
        try:
            self.model.replace_paper(self.library.update_flags(paper_id, notes=text))
        except KeyError:  # deleted meanwhile
            pass

    def _mark_reviewed(self) -> None:
        pid = self.inspector.paper_id
        if pid is not None:
            self._refresh_paper(self.library.update_metadata(pid, needs_review=False))
            if self.node[0] == NodeKind.REVIEW:
                self._schedule_reload()

    def lookup_selected(self, kind: str = "auto") -> None:
        papers = self.selected_papers()
        if len(papers) != 1:
            self.notify("Select a single document to look up its details")
            return
        paper = papers[0]
        doi = paper.meta.doi if kind in ("auto", "doi") else ""
        arxiv_id = paper.meta.arxiv_id if kind in ("auto", "arxiv") else ""
        title = paper.meta.title if kind == "auto" else ""
        if not (doi or arxiv_id or title):
            self.notify("Enter a DOI, an arXiv ID or a title first")
            return
        self.notify("Looking up details online...")
        pid = paper.id
        self.runner.submit(
            self.library.resolver.lookup,
            doi=doi,
            arxiv_id=arxiv_id,
            title=title,
            on_done=lambda result: self._on_lookup_done(pid, result),
            on_error=lambda message: self.notify(f"Lookup failed: {message}"),
        )

    def _on_lookup_done(self, paper_id: int, result: Any) -> None:
        meta, source = result
        if meta is None:
            self.notify("No matching record found online")
            return
        try:
            paper = self.library.apply_metadata(paper_id, meta)
        except (DuplicateDoiError, LibraryError, ValueError, KeyError) as exc:
            QMessageBox.warning(self, "Cannot apply details", str(exc))
            return
        self._refresh_paper(paper)
        self.notify(f"Details updated from {source}: {paper.citation_key}")

    # ================================================================== folders
    def _target_folder(self) -> int | None:
        return self.node[1] if self.node[0] == NodeKind.FOLDER else None

    def new_folder(self, parent_id: int | None = None) -> None:
        title = "New Sub-folder" if parent_id else "New Folder"
        name, ok = QInputDialog.getText(self, title, "Folder name:")
        if not ok or not name.strip():
            return
        try:
            collection = self.library.create_collection(name, parent_id=parent_id)
        except (ValueError, KeyError) as exc:
            QMessageBox.warning(self, title, str(exc))
            return
        self.reload_tree(select=(NodeKind.FOLDER, collection.id))
        self.notify(f"Created folder '{collection.name}' → bibtex/{collection.slug}.bib")

    def remove_folder(self, collection_id: int | None = None) -> None:
        cid = collection_id if collection_id is not None else self._target_folder()
        if cid is None:
            self.notify("Select a folder to remove")
            return
        name = self.tree.collection_name(cid)
        answer = QMessageBox.question(
            self,
            "Remove Folder",
            f"Remove the folder '{name}' and its sub-folders?\n\n"
            "Documents stay in your library and their PDFs stay on disk; only the folder and its .bib file "
            "inside the library are removed.",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.library.delete_collection(cid)
        self.reload_tree(select=(NodeKind.ALL, None))
        self.notify(f"Removed folder '{name}'")

    def _rename_folder(self, collection_id: int, name: str) -> None:
        try:
            collection = self.library.rename_collection(collection_id, name)
            self.notify(f"Renamed folder → bibtex/{collection.slug}.bib")
        except (ValueError, KeyError) as exc:
            QMessageBox.warning(self, "Rename Folder", str(exc))
        QTimer.singleShot(0, lambda: self.reload_tree(select=(NodeKind.FOLDER, collection_id)))

    def sync_bibs(self) -> None:
        self.library.sync_all_bibs()
        count = len(self.library.collections())
        self.notify(f"Synchronised {count} folder .bib file(s) and master.bib")

    def edit_view_settings(self, node: Node | None = None) -> None:
        kind, cid = node or self.node
        collection = self.library.db.get_collection(cid) if kind == NodeKind.FOLDER and cid is not None else None
        header = self.table.horizontalHeader()
        visible = {int(col) for col in Col if not header.isSectionHidden(int(col))}
        dialog = ViewSettingsDialog(
            self,
            title=collection.name if collection else NODE_TITLES.get(kind, "All Documents"),
            visible=visible,
            collection=collection,
            internal_bib=self.library.bib.collection_path(collection.slug) if collection else None,
        )
        if dialog.exec() != ViewSettingsDialog.DialogCode.Accepted:
            return
        for col in Col:
            header.setSectionHidden(int(col), int(col) not in dialog.visible_columns())
        if collection is not None and dialog.external_path() != (collection.bib_export_path or None):
            self.set_export_path(collection, dialog.external_path())

    def set_export_path(self, collection: Collection, path: str | None) -> None:
        try:
            self.library.set_collection_export_path(collection.id, path)
        except ForeignBibFileError:
            answer = QMessageBox.question(
                self,
                "Replace existing .bib file?",
                f"{path} already exists and was not created by {APP_NAME}.\n\n"
                f"Replace it with the entries of '{collection.name}'? Its current content will be lost.",
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
            self.library.set_collection_export_path(collection.id, path, overwrite_foreign=True)
        except (ValueError, FileNotFoundError) as exc:
            QMessageBox.warning(self, "External .bib file", str(exc))
            return
        self.notify(f"'{collection.name}' now also writes {path}" if path else "External .bib target removed")

    # ================================================================== drag & drop and imports
    def _on_tree_drop(self, mime: Any, kind: NodeKind, collection_id: int | None) -> bool:
        ids = paper_ids_from_mime(mime)
        if ids:
            if kind == NodeKind.FOLDER and collection_id is not None:
                self.add_to_folder(ids, collection_id)
            elif kind == NodeKind.FAVORITES:
                self._apply_flags(ids, favorite=True)
            elif kind == NodeKind.MINE:
                self._apply_flags(ids, my_publication=True)
            return True
        paths = [u.toLocalFile() for u in mime.urls() if u.isLocalFile()]
        self.import_paths(paths, collection_id if kind == NodeKind.FOLDER else None)
        return True

    def add_files_dialog(self) -> None:
        start = str(self.settings.value("import/lastDir", str(Path.home())))
        paths, _ = QFileDialog.getOpenFileNames(self, "Add Files", start, "PDF files (*.pdf)")
        if paths:
            self.settings.setValue("import/lastDir", str(Path(paths[0]).parent))
            self.import_paths(paths, self._target_folder())

    def add_folder_dialog(self) -> None:
        start = str(self.settings.value("import/lastDir", str(Path.home())))
        folder = QFileDialog.getExistingDirectory(self, "Add Folder", start)
        if folder:
            self.settings.setValue("import/lastDir", folder)
            self.import_paths([folder], self._target_folder())

    def import_paths(self, paths: Iterable[str | Path], collection_id: int | None = None) -> int:
        pdfs = expand_pdf_paths(paths)
        if not pdfs:
            self.notify("No PDF files found")
            return 0
        self.importer.import_files(pdfs, collection_id)
        self.notify(f"Importing {len(pdfs)} document(s)...")
        return len(pdfs)

    def _on_import_progress(self, done: int, total: int) -> None:
        self.progress.setRange(0, max(total, 1))
        self.progress.setValue(done)
        self.progress.setVisible(done < total)

    def _on_imported(self, result: IngestResult) -> None:
        key = result.paper.citation_key
        if result.status is IngestStatus.IMPORTED:
            self.notify(f"Imported {key}" + (" (needs review)" if result.paper.needs_review else ""))
        elif result.status is IngestStatus.DUPLICATE_DOI:
            self.notify(f"Same DOI already in the library: {key}")
        else:
            self.notify(f"Already in the library: {key}")
        self._schedule_reload()

    def _on_import_failed(self, path: str, message: str) -> None:
        self.notify(f"Could not import {Path(path).name}: {message}")

    def _on_import_finished(self, new: int, duplicates: int, failures: int) -> None:
        self.progress.hide()
        parts = [f"{new} imported"]
        if duplicates:
            parts.append(f"{duplicates} already in library")
        if failures:
            parts.append(f"{failures} failed")
        self.notify("Import finished: " + ", ".join(parts))

    def _schedule_reload(self) -> None:
        self._reload_timer.start()

    # ================================================================== Mendeley import, naming rule
    def import_mendeley_dialog(self) -> None:
        if self.mendeley.running:
            self.notify("A Mendeley import is already running")
            return
        start = Path(str(self.settings.value("import/lastBibDir", str(Path.home()))))
        dialog = MendeleyImportDialog(self, start_dir=start)
        if dialog.exec() != MendeleyImportDialog.DialogCode.Accepted:
            return
        options = dialog.options()
        self.settings.setValue("import/lastBibDir", str(options.bib_paths[0].parent))
        self.mendeley.start(options)
        self.notify("Reading the Mendeley export...")

    def _on_mendeley_planned(self, plan: MendeleyImportPlan) -> None:
        self.notify("Importing " + summarize(plan) + "...")

    def _on_mendeley_finished(self, report: MendeleyImportReport) -> None:
        self.progress.hide()
        self.reload_tree(select=self.node)
        self.reload_view(keep_selection=False)
        self.notify(f"Mendeley import finished: {report.imported} imported, {report.duplicates} already present")
        if self.isVisible():
            show_import_report(self, report)

    def apply_naming_rule(self) -> None:
        outdated = self.library.papers_with_outdated_keys()
        if not outdated:
            self.notify("Every citation key already follows the naming rule")
            return
        if not confirm_naming_rule(self, outdated):
            return
        try:
            renamed = self.library.apply_naming_rule(p.id for p, _ in outdated)
        except (LibraryError, OSError, ValueError) as exc:
            QMessageBox.warning(self, "Apply Naming Rule", str(exc))
            renamed = []
        self.reload_view(keep_selection=True)
        self.notify(f"Renamed {len(renamed)} citation key(s)")

    def store_linked(self, ids: Iterable[int]) -> None:
        done = 0
        for pid in ids:
            try:
                self.library.move_into_storage(pid, mode="copy")
                done += 1
            except (LibraryError, OSError) as exc:
                QMessageBox.warning(self, "Copy into Library", str(exc))
                break
        self.reload_view(keep_selection=True)
        self.notify(f"Copied {done} linked PDF(s) into pdfs/<year>/ (originals left in place)")

    # ================================================================== attaching PDFs
    def _on_files_dropped(self, paths: list[str], row: int) -> None:
        """Files dropped on the list. Onto a row: one PDF on a document without PDF becomes its PDF; other files
        (not PDFs, or several) can become its supplementary files. Elsewhere: PDFs are imported as documents."""
        pdfs = expand_pdf_paths(paths)
        files = [Path(p) for p in paths if Path(p).is_file()]
        paper = None
        if row >= 0:
            pid = self.proxy.index(row, 0).data(PAPER_ID_ROLE)
            paper = self.library.get_paper(int(pid)) if pid is not None else None
        if paper is not None:
            if len(files) == 1 and len(pdfs) == 1 and not paper.has_pdf:
                self.attach_files(paper.id, pdfs[0])
                return
            non_pdf = [f for f in files if f.suffix.lower() != ".pdf"]
            choice = "supplement" if non_pdf and not pdfs else self._ask_drop_target(paper, files)
            if choice == "supplement":
                main = None if paper.has_pdf else next((f for f in files if f.suffix.lower() == ".pdf"), None)
                if main is not None:  # a document without PDF: the first PDF becomes its PDF
                    self.attach_files(paper.id, main)
                self.attach_supplements(paper.id, [f for f in files if f != main])
                return
            if choice is None:
                return
        self.import_paths(paths, self._target_folder())

    def _ask_drop_target(self, paper: Paper, files: list[Path]) -> str | None:
        """"supplement" | "import" | None (cancelled) for files dropped onto a document's row."""
        box = QMessageBox(self)
        box.setWindowTitle("Files dropped on a document")
        n = len(files)
        box.setText(f"{n} file{'s' if n != 1 else ''} dropped on {paper.citation_key}.")
        box.setInformativeText(
            "Attach them to this document as supplementary files, or import the PDFs as new documents?"
            + ("" if paper.has_pdf else "\nThe first PDF becomes the document's PDF.")
        )
        attach = box.addButton("Attach to This Document", QMessageBox.ButtonRole.AcceptRole)
        new = box.addButton("Import as New Documents", QMessageBox.ButtonRole.ActionRole)
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(attach)
        box.exec()
        clicked = box.clickedButton()
        return "supplement" if clicked is attach else "import" if clicked is new else None

    def attach_pdf_dialog(self) -> None:
        papers = self.selected_papers()
        if len(papers) != 1 or papers[0].has_pdf:
            self.notify("Select one document without a PDF")
            return
        start = str(self.settings.value("import/lastDir", str(Path.home())))
        path, _ = QFileDialog.getOpenFileName(
            self, f"Attach PDF to {papers[0].citation_key}", start, "PDF files (*.pdf)"
        )
        if path:
            self.settings.setValue("import/lastDir", str(Path(path).parent))
            self.attach_files(papers[0].id, Path(path))

    def attach_files(self, paper_id: int, path: Path, mode: str | None = None) -> None:
        if self.attacher.running:
            self.notify("Another attach operation is still running")
            return
        self._attach_search = False
        self.attacher.attach([(paper_id, path)], mode)
        self.notify(f"Attaching {path.name}...")

    # ================================================================== supplementary files
    def add_supplements_dialog(self) -> None:
        papers = self.selected_papers()
        if len(papers) != 1:
            self.notify("Select one document")
            return
        start = str(self.settings.value("import/lastDir", str(Path.home())))
        paths, _ = QFileDialog.getOpenFileNames(
            self, f"Supplementary files for {papers[0].citation_key}", start, "All files (*)"
        )
        if paths:
            self.settings.setValue("import/lastDir", str(Path(paths[0]).parent))
            self.attach_supplements(papers[0].id, [Path(p) for p in paths])

    def attach_supplements(self, paper_id: int, paths: list[Path], mode: str | None = None) -> None:
        """Hash and read the files in the background, then attach them (GUI thread, as for imports)."""
        if not paths:
            return
        self.notify(f"Attaching {len(paths)} supplementary file{'s' if len(paths) != 1 else ''}...")
        for path in paths:
            self.runner.submit(
                self.library.prepare_attachment,
                path,
                on_done=partial(self._commit_supplement, paper_id=paper_id, mode=mode),
                on_error=partial(self._supplement_failed, path),
            )

    def _commit_supplement(self, candidate: Any, paper_id: int, mode: str | None) -> None:
        try:
            attachment = self.library.add_supplement(paper_id, candidate, mode=mode)
        except (LibraryError, OSError, KeyError) as exc:
            self._supplement_failed(Path(candidate.path), str(exc))
            return
        self._refresh_paper(self.library.get_paper(paper_id))
        self.notify(f"Supplementary file {attachment.file_name} attached")

    def _supplement_failed(self, path: Path, message: str) -> None:
        QMessageBox.warning(self, "Supplementary file", f"{Path(path).name}: {message}")

    def _on_attachment_action(self, action: str, attachment_id: int) -> None:
        try:
            attachment = self.library.get_attachment(attachment_id)
        except KeyError:
            return
        path = self.library.attachment_path(attachment)
        if action in ("open", "reveal") and not path.is_file():
            QMessageBox.warning(self, "File not found", f"The file is not on this computer:\n{path}")
            return
        if action == "open":
            self._launch(QUrl.fromLocalFile(str(path)))
        elif action == "reveal":
            self._reveal(path)
        elif action == "rename":
            text, ok = QInputDialog.getText(self, "Rename", "Name shown for this file:", text=attachment.display_name)
            if ok and text.strip():
                self.library.rename_supplement(attachment.id, text)
                self._refresh_paper(self.library.get_paper(attachment.paper_id))
        elif action == "promote":
            try:
                paper = self.library.promote_supplement(attachment.id)
            except (LibraryError, OSError) as exc:
                QMessageBox.warning(self, "Set as Main PDF", str(exc))
                return
            self._outline_for = None  # the table of contents is now the one of the new main PDF
            self._refresh_paper(paper)
            self.notify(f"{attachment.display_name} is now the PDF of {paper.citation_key}")
        elif action == "remove":
            self.remove_supplement(attachment)

    def remove_supplement(self, attachment: Attachment) -> None:
        detail = (
            "The file is left where it is (linked file)."
            if attachment.linked
            else f"The file {attachment.file_name} is deleted from the library folder."
        )
        answer = QMessageBox.question(self, "Remove supplementary file", f"Remove \"{attachment.display_name}\"?\n\n{detail}")
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.library.remove_supplement(attachment.id)
        paper_id = attachment.paper_id
        self._refresh_paper(self.library.get_paper(paper_id))
        sources = self.library.fulltext_sources(paper_id)  # the full-text entry is rebuilt without its text
        self.runner.submit(
            self.library.read_fulltext,
            sources,
            on_done=partial(self._set_fulltext, paper_id),
            on_error=lambda message: log.warning("Full text not rebuilt: %s", message),
        )

    def _set_fulltext(self, paper_id: int, text: str) -> None:
        try:
            self.library.set_fulltext(paper_id, text)
        except KeyError:  # deleted meanwhile
            pass

    # ================================================================== Ask AI
    def _save_assistant_settings(self) -> None:
        try:
            self.library.config.save_user_settings(self.user_config)
        except OSError as exc:
            log.warning("Could not save the Ask AI settings: %s", exc)

    def _on_assistant_notes(self, paper: Paper) -> None:
        self._refresh_paper(paper)
        self.notify(f"Answer saved in the notes of {paper.citation_key}")

    def find_missing_pdfs(self) -> None:
        if self.attacher.running:
            self.notify("Another attach operation is still running")
            return
        missing = len(self.library.papers_without_pdf())
        if not missing:
            self.notify("Every document has a PDF")
            return
        last = self.settings.value("attach/lastFolder", "")
        folder = Path(str(last)) if last else default_mendeley_pdf_folder()
        dialog = FindPdfsDialog(self, folder=folder, missing=missing)
        if dialog.exec() != FindPdfsDialog.DialogCode.Accepted:
            return
        folder, mode = dialog.values()
        self.settings.setValue("attach/lastFolder", str(folder))
        self._attach_search = True
        self.attacher.find_missing([folder], mode)
        self.notify(f"Searching {folder} for missing PDFs...")

    def _on_pdf_attached(self, paper: Paper) -> None:
        if not self._attach_search:
            self._refresh_paper(paper)
            self.notify(f"PDF attached to {paper.citation_key}")

    def _on_attach_finished(self, report: AttachReport) -> None:
        self.progress.hide()
        if self._attach_search:
            self.reload_view(keep_selection=True)
            self.notify(f"Find Missing PDFs: {len(report.attached)} attached")
            if self.isVisible():
                show_attach_report(self, report, search=True)
        elif report.failed:
            QMessageBox.warning(self, "Attach PDF", "\n".join(report.failed))
        self._attach_search = False

    # ================================================================== inbox (Milestone 4)
    def restart_inbox(self) -> None:
        self.stop_inbox()
        config = self.library.config
        enabled = config.inbox_enabled if self._watch_inbox is None else self._watch_inbox
        if not enabled:
            return
        self.inbox = InboxWatcher(config.inbox_path, self.inbox_bridge.emit_path, settle_s=self.INBOX_SETTLE_S)
        try:
            self.inbox.start()
        except OSError as exc:
            self.inbox = None
            self.notify(f"Cannot watch the inbox folder: {exc}")

    def stop_inbox(self) -> None:
        if self.inbox is not None:
            self.inbox.stop()
            self.inbox = None

    def _on_inbox_file(self, path: str) -> None:
        if self.inbox is None:
            return
        self.notify(f"Inbox: importing {Path(path).name}...")
        self.importer.import_files([Path(path)], None, mode="move", inbox=self.inbox.folder)

    def open_inbox_folder(self) -> None:
        folder = self.library.config.inbox_path
        folder.mkdir(parents=True, exist_ok=True)
        self._launch(QUrl.fromLocalFile(str(folder)))

    # ================================================================== menus
    @staticmethod
    def _menu_action(menu: QMenu, text: str, slot: Callable[[], Any], icon: QIcon | None = None) -> QAction:
        action = menu.addAction(text)
        if icon is not None:
            action.setIcon(icon)
        action.triggered.connect(lambda _checked=False: slot())
        return action

    def _folder_entries(self) -> list[tuple[Collection, int]]:
        collections = self.library.collections()
        children: dict[int | None, list[Collection]] = {}
        for c in collections:
            children.setdefault(c.parent_id, []).append(c)
        ordered: list[tuple[Collection, int]] = []

        def walk(parent: int | None, depth: int) -> None:
            for c in sorted(children.get(parent, []), key=lambda c: c.name.casefold()):
                ordered.append((c, depth))
                walk(c.id, depth + 1)

        walk(None, 0)
        return ordered

    def _table_menu(self, pos: QPoint) -> None:
        papers = self.selected_papers()
        if not papers:
            return
        ids = [p.id for p in papers]
        menu = QMenu(self)
        menu.setDefaultAction(self._menu_action(menu, "Open File", self.open_selected))
        self._menu_action(menu, "Show in Folder", self.show_selected_in_folder)
        menu.addSeparator()
        menu.addAction(self.act_copy_cite)
        menu.addAction(self.act_copy_bibtex)
        menu.addSeparator()
        mark = menu.addMenu("Mark As")
        self._menu_action(mark, "Read", lambda: self._apply_flags(ids, is_read=True))
        self._menu_action(mark, "Unread", lambda: self._apply_flags(ids, is_read=False))
        mark.addSeparator()
        favorite = not all(p.favorite for p in papers)
        self._menu_action(mark, "Favorite" if favorite else "Not Favorite",
                          lambda: self._apply_flags(ids, favorite=favorite), icons.star(True))  # fmt: skip
        mine = not all(p.my_publication for p in papers)
        self._menu_action(mark, "My Publication" if mine else "Not My Publication",
                          lambda: self._apply_flags(ids, my_publication=mine), icons.person())  # fmt: skip
        folders = menu.addMenu(icons.folder(), "Add to Folder")
        entries = self._folder_entries()
        if not entries:
            folders.addAction("(no folders yet)").setEnabled(False)
        for collection, depth in entries:
            self._menu_action(folders, "    " * depth + collection.name,
                              partial(self.add_to_folder, ids, collection.id), icons.folder())  # fmt: skip
        if len(papers) == 1:
            self._fill_search_menu(menu.addMenu(icons.search(), "Search For"), papers[0])
        if self.node[0] == NodeKind.FOLDER:
            self._menu_action(menu, "Remove from This Folder", self.remove_from_current_folder)
        menu.addSeparator()
        lookup = self._menu_action(menu, "Update Details Online", lambda: self.lookup_selected("auto"))
        lookup.setEnabled(len(papers) == 1)
        if len(papers) == 1 and not papers[0].has_pdf:
            menu.addAction(self.act_attach_pdf)
        if len(papers) == 1:
            menu.addAction(self.act_add_supplements)
            if papers[0].attachments:
                supplements = menu.addMenu(icons.paperclip(), "Supplementary Files")
                for attachment in self.library.attachments(papers[0].id):
                    self._menu_action(
                        supplements,
                        attachment.display_name,
                        partial(self._on_attachment_action, "open", attachment.id),
                        icons.pdf_file() if attachment.is_pdf else icons.paperclip(),
                    )
        linked = [p.id for p in papers if p.pdf_linked]
        if linked:
            self._menu_action(menu, "Copy Linked PDF into Library Storage", partial(self.store_linked, linked))
        menu.addSeparator()
        menu.addAction(self.act_delete)
        menu.exec(self.table.viewport().mapToGlobal(pos))

    def _fill_search_menu(self, menu: QMenu, paper: Paper) -> None:
        """ "More like this": author / journal / year / tag queries for the clicked document."""

        def quoted(value: str) -> str:
            return f'"{value}"' if " " in value or ":" in value else value

        entries: list[tuple[str, str]] = []
        for author in paper.meta.authors[:6]:
            name = author.family or author.literal
            entries.append((f"Author: {author.display()}", f"author:{quoted(name)}"))
        if paper.meta.journal:
            entries.append((f"Published in: {paper.meta.journal}", f"journal:{quoted(paper.meta.journal)}"))
        if paper.meta.year is not None:
            entries.append((f"Year: {paper.meta.year}", f"year:{paper.meta.year}"))
        for tag in split_list_field(paper.tags)[:6]:
            entries.append((f"Tag: {tag}", f"tag:{quoted(tag)}"))
        for label, query in entries:
            self._menu_action(menu, label, partial(self.search_for, query))
        if not entries:
            menu.addAction("(no author, journal or year)").setEnabled(False)

    def search_for(self, query: str) -> None:
        """Show every document matching ``query`` (e.g. ``author:anand``), across the whole library."""
        self.set_search_scope("all")
        self.tree.select_node(NodeKind.ALL, None)
        self.search.setText(query)
        self._apply_search()
        self.search.setFocus()

    def _tree_menu(self, pos: QPoint) -> None:
        node = self.tree.node_at(pos)
        menu = QMenu(self)
        if node is not None and node[0] == NodeKind.FOLDER and node[1] is not None:
            cid = node[1]
            collection = self.library.db.get_collection(cid)
            self._menu_action(menu, "New Sub-folder...", lambda: self.new_folder(cid), icons.folder_add())
            self._menu_action(menu, "Rename Folder",
                              lambda: self.tree.edit(self.tree.find_index(NodeKind.FOLDER, cid)))  # fmt: skip
            self._menu_action(menu, "Remove Folder...", lambda: self.remove_folder(cid), icons.folder_remove())
            menu.addSeparator()
            self._menu_action(menu, "BibTeX Export Settings...", lambda: self.edit_view_settings(node))
            if collection is not None:
                bib = self.library.bib.collection_path(collection.slug)
                self._menu_action(menu, f"Open {bib.name}", lambda: self._launch(QUrl.fromLocalFile(str(bib))))
                self._menu_action(menu, f"Show {bib.name} in Folder", lambda: self._reveal(bib))
        else:
            self._menu_action(menu, "New Folder...", self.new_folder, icons.folder_add())
            if node is not None and node[0] == NodeKind.ALL:
                self._menu_action(menu, "Synchronise .bib Files", self.sync_bibs, icons.sync())
        menu.exec(self.tree.viewport().mapToGlobal(pos))

    def _fill_columns_menu(self, menu: QMenu) -> None:
        menu.clear()
        header = self.table.horizontalHeader()
        for col in Col:
            action = menu.addAction(COLUMN_NAMES[col])
            action.setCheckable(True)
            action.setChecked(not header.isSectionHidden(int(col)))
            action.setEnabled(col != Col.TITLE)
            action.toggled.connect(partial(self._set_column_visible, int(col)))

    def _set_column_visible(self, column: int, visible: bool) -> None:
        self.table.horizontalHeader().setSectionHidden(column, not visible)

    def _header_menu(self, pos: QPoint) -> None:
        menu = QMenu(self)
        self._fill_columns_menu(menu)
        menu.exec(self.table.horizontalHeader().mapToGlobal(pos))

    # ================================================================== misc
    def _set_filter_visible(self, visible: bool) -> None:
        self.filter_pane.setVisible(visible)
        if self.filter_toggle.isChecked() != visible:
            self.filter_toggle.setChecked(visible)
        if not visible:
            self.proxy.set_facet(None, None)
        else:
            self.proxy.set_facet(self.filter_pane.kind, self.filter_pane.selected_value())

    def _set_details_visible(self, visible: bool) -> None:
        self.inspector.setVisible(visible)

    # ================================================================== theme and preferences
    def apply_theme(self, mode: str, *, rebuild: bool = True) -> None:
        app = QApplication.instance()
        assert isinstance(app, QApplication)
        self.theme_mode = mode if mode in theme.THEME_MODES else "system"
        current = theme.apply(app, self.theme_mode)
        for action, icon in (
            (self.act_add_primary, icons.add(on_accent=True)),
            (self.act_add_files, icons.document()),
            (self.act_new_folder, icons.folder_add()),
            (self.act_remove_folder, icons.folder_remove()),
            (self.act_sync, icons.sync()),
            (self.act_help, icons.help_circle()),
            (self.act_preferences, icons.settings()),
            (self.act_copy_cite, icons.copy()),
            (self.act_delete, icons.delete()),
            (self.act_lookup, icons.search()),
            (self.act_show_in_folder, icons.open_link()),
        ):
            action.setIcon(icon)
        self.search_mode_action.setIcon(icons.search())
        self.filter_toggle.setIcon(icons.funnel())
        self.edit_settings.setIcon(icons.settings())
        self.table.empty_state.refresh()
        self.table.update_row_height()
        self.inspector.refresh_icons()
        theme.style_window_frame(self, current)
        if rebuild:
            self.tree.rebuild(self.library.collections(), select=self.node)
            self._refresh_counts()
            self.table.viewport().update()

    def _on_system_scheme_changed(self, *_args: Any) -> None:
        if self.theme_mode == "system":
            self.apply_theme("system")

    def open_preferences(self) -> None:
        config = self.library.config
        dialog = PreferencesDialog(self, config=config, theme_mode=self.theme_mode)
        if dialog.exec() != PreferencesDialog.DialogCode.Accepted:
            return
        self.apply_preferences(dialog.values())

    def apply_preferences(self, values: Any) -> None:
        old = self.library.config
        base_words = tuple(w for w in old.protected_words if w not in old.extra_protected_words)
        new = dataclasses.replace(
            old,
            library_root=values.library_root,
            protected_words=base_words,
            import_mode=values.import_mode,
            contact_email=values.contact_email,
            extra_protected_words=values.extra_protected_words,
            inbox_enabled=values.inbox_enabled,
            inbox_folder=values.inbox_folder,
        )
        new.save_user_settings(self.user_config)
        self.settings.setValue("view/theme", values.theme_mode)
        if values.theme_mode != self.theme_mode:
            self.apply_theme(values.theme_mode)
        if new.library_root != old.library_root:
            self.libraryChangeRequested.emit(new)
            return
        self.library.config = new
        if (new.inbox_enabled, new.inbox_path) != (old.inbox_enabled, old.inbox_path):
            self.restart_inbox()
        if self.library._resolver is not None:
            self.library._resolver.user_agent = new.user_agent
        if new.protected_words != old.protected_words:
            self.library.bib.protected_words = new.protected_words
            self.library.sync_all_bibs()
            self.notify("Preferences saved; .bib files rewritten with the new protected words")
        else:
            self.notify("Preferences saved")

    # ================================================================== sharing between devices
    def _update_device_label(self) -> None:
        engine = self.library.sync
        self.act_sync_devices.setEnabled(engine is not None)
        if engine is None:
            self.device_label.setText("")
            return
        others = max(0, len(engine.devices()) - 1)
        when = datetime.fromtimestamp(engine.last_sync).strftime("%H:%M") if engine.last_sync else "\u2014"
        self.device_label.setText(f"Shared \u00b7 {others} other device{'' if others == 1 else 's'} \u00b7 synced {when}  ")

    def _device_sync_tick(self) -> None:
        if self.isVisible():
            self.sync_devices(manual=False)

    def sync_devices(self, *, manual: bool = False) -> None:
        """Journal local changes and apply the other devices' changes (timer, F6, window activation)."""
        library = self.library
        if library.sync is None:
            if manual:
                self.device_sync_dialog()
            return
        if self.mendeley.running or self.importer.pending or library._bulk_depth:
            return  # a bulk import is writing: its changes are journaled when it is done
        self.inspector.flush_notes()
        try:
            report = library.sync_now()
        except Exception as exc:  # e.g. the folder is temporarily unavailable
            log.exception("Device synchronisation failed")
            if manual:
                QMessageBox.warning(self, APP_NAME, f"Synchronisation failed:\n\n{exc}")
            return
        self._update_device_label()
        if report.changed:
            self.reload_tree(select=self.node)
            self.reload_view(keep_selection=True)
            self._refresh_counts()
        if report.changed or manual:
            self.notify(f"Devices: {report.summary()}")
        if report.duplicates:
            pairs = ", ".join(f"{a} / {b}" for a, b in report.duplicates[:5])
            self.notify(f"Added on two devices (both kept, delete one if they are the same): {pairs}")

    def device_sync_dialog(self) -> None:
        library = self.library
        dialog = DeviceSyncDialog(self, library)
        if dialog.exec() != DeviceSyncDialog.DialogCode.Accepted:
            return
        values = dialog.values()
        same_folder = values.folder.resolve() == Path(library.root).resolve()
        if library.sync is not None and same_folder and values.device_name in ("", library.sync.device_name):
            self.sync_devices(manual=True)
            return
        if self.mendeley.running or self.importer.pending:
            QMessageBox.information(self, APP_NAME, "Wait for the running import to finish first.")
            return
        if not same_folder:
            answer = QMessageBox.question(
                self,
                "Move the library",
                f"Use this folder as the library folder?\n\n{values.folder}\n\n"
                f"{APP_NAME} moves its own pdfs/, bibtex/ and sync/ folders there. Linked PDFs are not copied "
                "or moved: they stay where they are.",
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        self.inspector.flush_notes()
        self._reload_timer.stop()
        self._device_timer.stop()
        self.stop_inbox()
        try:
            if same_folder:
                config = library.config
                library.close()  # the database file moves out of the folder when the library is reopened shared
            else:
                config = library.relocate(values.folder)
        except Exception as exc:
            QMessageBox.warning(self, APP_NAME, f"The library could not be moved:\n\n{exc}")
            self.restart_inbox()
            return
        config = dataclasses.replace(
            config,
            protected_words=tuple(w for w in config.protected_words if w not in config.extra_protected_words),
            sync_enabled=True,
            device_name=values.device_name,
        )
        config.save_user_settings(self.user_config)
        self.libraryChangeRequested.emit(config)

    def show_help(self) -> None:
        show_about(self, self.library.root)

    def notify(self, message: str) -> None:
        self.status_label.setText(message)
        log.info(message)
