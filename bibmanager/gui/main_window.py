"""Main window: the shelf (left, navy), the top bar, the header of the current view with the manuscript check, the
filter chips, the reference list (rows or table) and the reference sheet; Study mode; menus, shortcuts and every
user action.

All mutations go through ``core.library.Library``; slow work goes through ``TaskRunner``.
"""

from __future__ import annotations

import dataclasses
import logging
import os
import sys
from collections.abc import Callable, Iterable
from datetime import date, datetime
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
    QStringListModel,
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
    QCompleter,
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
    QSplitter,
    QStackedWidget,
    QTableView,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from .. import APP_NAME, APP_SUBTITLE
from ..core.config import user_config_path
from ..core.extractor import pdf_outline
from ..core.inbox import InboxWatcher
from ..core.library import DuplicateDoiError, ForeignBibFileError, IngestResult, IngestStatus, Library, LibraryError
from ..core.manuscript import CitationScan, ManuscriptCheck, compare, manuscript_folder, scan_citations
from ..core.mendeley import MendeleyImportPlan, MendeleyImportReport, default_mendeley_pdf_folder, summarize
from ..core.models import Attachment, Collection, Paper, split_list_field
from ..core.query import SCOPE_LABELS, SCOPES, SYNTAX_HELP, Query, parse_query
from ..core.secrets import KeyStore
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
from .filters import FilterBar, ViewHeader
from .inspector import ReferenceSheet
from .paper_model import (
    COLUMN_NAMES,
    DEFAULT_HIDDEN,
    DEFAULT_ORDER,
    DEFAULT_WIDTHS,
    ICON_COLUMNS,
    PAPER_ID_ROLE,
    TABLE_COLUMNS,
    Col,
    IconDelegate,
    PaperFilterProxy,
    PaperTableModel,
    ReferenceDelegate,
    TwoLineDelegate,
    paper_ids_from_mime,
)
from .sidebar import NODE_TITLES, CollectionTree, Node, NodeKind, Shelf
from .study import StudyView
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
VIEW_ROWS, VIEW_TABLE = "rows", "table"
# sort key -> (menu label, column, order)
SORTS: dict[str, tuple[str, Col, Qt.SortOrder]] = {
    "year": ("Year, newest first", Col.YEAR, Qt.SortOrder.DescendingOrder),
    "year_asc": ("Year, oldest first", Col.YEAR, Qt.SortOrder.AscendingOrder),
    "added": ("Recently added", Col.ADDED, Qt.SortOrder.DescendingOrder),
    "opened": ("Recently opened", Col.OPENED, Qt.SortOrder.DescendingOrder),
    "title": ("Title, A to Z", Col.TITLE, Qt.SortOrder.AscendingOrder),
    "author": ("First author, A to Z", Col.AUTHORS, Qt.SortOrder.AscendingOrder),
    "key": ("Citation key, A to Z", Col.CITATION_KEY, Qt.SortOrder.AscendingOrder),
}
DEFAULT_SORT = "added"


# --------------------------------------------------------------------------- reference list
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
        self.button = QPushButton("Import PDFs")
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
        self.picture.setPixmap(icons.app_icon().pixmap(72, 72))

    def show_for(self, *, filtered: bool, inbox: bool = False) -> None:
        if inbox and not filtered:
            self.title.setText("Nothing to verify")
            self.text.setText("References whose details could not be confirmed online wait here after an import.")
        elif filtered:
            self.title.setText("No matching references")
            self.text.setText("Try another search, or clear the filters.")
        else:
            self.title.setText("No references here yet")
            self.text.setText("Drag and drop PDF files here, or import them from your computer.")
        self.button.setVisible(not filtered and not inbox)
        self.show()


class DocumentTable(QTableView):
    """The reference list. Rows view: one painted cell per reference (key, title, authors, venue, glyphs); table
    view: sortable columns. Drags references out (to projects, or ``\\cite{}`` into an editor), accepts files in."""

    def __init__(self, parent: QWidget | None = None, on_add: Callable[[], object] = lambda: None) -> None:
        super().__init__(parent)
        self.setObjectName("documents")
        self.files_dropped: Callable[[list[str], int], object] | None = None  # (paths, row under the cursor)
        self.hover_row = -1
        self.mode = VIEW_ROWS
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
        self.reference_delegate = ReferenceDelegate(self)
        self.setItemDelegateForColumn(int(Col.REFERENCE), self.reference_delegate)
        self.empty_state = EmptyState(self.viewport(), on_add)
        self.empty_state.hide()

    def update_row_height(self) -> None:
        self.ensurePolished()
        metrics = QFontMetrics(self.font())
        self.verticalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
        if self.mode == VIEW_ROWS:
            height = self.reference_delegate.row_height(self.font())
        else:
            height = 2 * metrics.lineSpacing() + 20
        self.verticalHeader().setDefaultSectionSize(height)
        self.horizontalHeader().setFixedHeight(metrics.height() + 16)

    def update_empty_state(self, *, empty: bool, filtered: bool, inbox: bool = False) -> None:
        if empty:
            self.empty_state.setGeometry(self.viewport().rect())
            self.empty_state.show_for(filtered=filtered, inbox=inbox)
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


def _display_path(path: str | Path) -> str:
    """A path for display, with the home folder shortened to ``~``."""
    text, home = str(path), str(Path.home())
    return "~" + text[len(home):] if home and text.startswith(home) else text


def _plain(text: str) -> str:
    """A menu/action text without accelerators and ellipsis, for the command palette."""
    return text.replace("&&", "\0").replace("&", "").replace("\0", "&").removesuffix("...").strip()


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
        self.view_mode = VIEW_ROWS
        self.sort_key = DEFAULT_SORT
        self.manuscript: ManuscriptCheck | None = None
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

        self.setWindowTitle(f"{APP_NAME} — {APP_SUBTITLE}")
        self.setWindowIcon(icons.app_icon())
        self.resize(1480, 920)
        self._create_actions()
        self._create_menus()
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
        self.act_add_files = a("Import PDFs...", self.add_files_dialog, shortcut="Ctrl+O",
                               tip="Import PDF files into the library (Ctrl+O)")  # fmt: skip
        self.act_add_primary = QAction("Import PDFs", self)
        self.act_add_primary.setToolTip("Import PDF files into the library (Ctrl+O)")
        self.act_add_primary.triggered.connect(lambda _checked=False: self.add_files_dialog())
        self.act_add_folder = a("Import a Folder of PDFs...", self.add_folder_dialog,
                                tip="Import every PDF in a folder")  # fmt: skip
        self.act_new_folder = a("New Project...", self.new_folder, shortcut="Ctrl+Shift+N",
                                tip="Create a project: a collection with its own .bib file")  # fmt: skip
        self.act_remove_folder = a("Remove Project...", self.remove_folder,
                                   tip="Remove the selected project (its references stay in the library)")  # fmt: skip
        self.act_sync = a("Rewrite .bib Files", self.sync_bibs, shortcut="F5",
                          tip="Rewrite the .bib file of every project and master.bib (F5)")  # fmt: skip
        self.act_help = a("Shortcuts && About", self.show_help, shortcut="F1", tip="Help: shortcuts and about")
        self.act_preferences = a("Preferences...", self.open_preferences, shortcut="Ctrl+,",
                                 tip="Preferences: library folder, theme, contact e-mail")  # fmt: skip
        self.act_quit = a("Quit", self.close, shortcut=QKeySequence.StandardKey.Quit)
        self.act_open = a("Open PDF", self.open_selected, shortcut="Return", context=table_only)
        self.act_show_in_folder = a("Show PDF in Folder", self.show_selected_in_folder)
        self.act_copy_cite = a("Copy \\cite{...}", self.copy_citation, shortcut="Ctrl+Shift+C")
        self.act_copy_bibtex = a("Copy BibTeX Entry", self.copy_bibtex, shortcut="Ctrl+Shift+B")
        self.act_delete = a("Delete References...", self.delete_selected, shortcut=QKeySequence.StandardKey.Delete,
                            context=table_only)  # fmt: skip
        self.act_select_all = a("Select All", lambda: self.table.selectAll())
        self.act_find = a("Search", self.focus_search, shortcut=QKeySequence.StandardKey.Find)
        self.act_command = a("Search or Run a Command", self.focus_search, shortcut="Ctrl+K",
                             tip="Type to search; start with > to run a command (Ctrl+K)")  # fmt: skip
        self.act_lookup = a("Look Up Metadata", lambda: self.lookup_selected("auto"),
                            tip="Fetch the details from Crossref / arXiv / OpenAlex")  # fmt: skip
        self.act_verify = a("Mark as Verified", self.mark_selected_verified, shortcut="Ctrl+Return",
                            tip="The details are right: take the references out of the Inbox (Ctrl+Return)")  # fmt: skip
        self.act_study = a("Study and Ask AI", self.enter_study, shortcut="Ctrl+J",
                           tip="Notes and questions to an AI model about the selected paper (Ctrl+J)")  # fmt: skip
        self.act_show_filter = a("Filter Chips", self._set_filter_visible, checkable=True)
        self.act_show_details = a("Reference Sheet", self._set_details_visible, checkable=True)
        self.view_mode_group = QActionGroup(self)
        self.act_view_rows = a("Rows", lambda: self.set_view_mode(VIEW_ROWS), shortcut="Ctrl+1")
        self.act_view_table = a("Table", lambda: self.set_view_mode(VIEW_TABLE), shortcut="Ctrl+2")
        for action in (self.act_view_rows, self.act_view_table):
            action.setCheckable(True)
            self.view_mode_group.addAction(action)
        self.act_open_library = a(
            "Open Library Folder", lambda: self._launch(QUrl.fromLocalFile(str(self.library.root)))
        )
        self.act_open_bib_dir = a(
            "Open BibTeX Folder", lambda: self._launch(QUrl.fromLocalFile(str(self.library.bib.bibtex_dir)))
        )
        self.act_edit_settings = a("Project Settings...", lambda: self.edit_view_settings())
        self.act_import_mendeley = a("Import from Mendeley...", self.import_mendeley_dialog,
                                     tip="Build the library from Mendeley's BibTeX export")  # fmt: skip
        self.act_apply_naming = a("Apply Naming Rule to Existing Keys...", self.apply_naming_rule,
                                  tip="Re-key references as Surname_Year, Surname1_Surname2_Year, ...")  # fmt: skip
        self.act_attach_pdf = a("Attach PDF...", self.attach_pdf_dialog,
                                tip="Choose the PDF of the selected reference, or drop a PDF onto its row")  # fmt: skip
        self.act_add_supplements = a("Add Supplementary Files...", self.add_supplements_dialog,
                                     tip="Attach supplementary information, data or videos to the selected reference")  # fmt: skip
        self.act_find_pdfs = a("Find Missing PDFs...", self.find_missing_pdfs,
                               tip="Search a folder for the PDFs of references that have none")  # fmt: skip
        self.act_open_inbox = a("Open Inbox Folder", self.open_inbox_folder,
                                tip="PDFs saved here are imported automatically")  # fmt: skip
        self.act_share = a("Share Library with Other Devices...", self.device_sync_dialog,
                           tip="Keep this library identical on another PC or the Android app (synced folder)")  # fmt: skip
        self.act_sync_devices = a("Synchronise Devices Now", lambda: self.sync_devices(manual=True), shortcut="F6",
                                  tip="Send this device's changes and apply those of the other devices (F6)")  # fmt: skip
        self.act_save_search = a("Save Current Search...", self.save_current_search,
                                 tip="Keep the search box and the filter chips as a saved search on the shelf")  # fmt: skip

    def _create_menus(self) -> None:
        bar = self.menuBar()
        file_menu = bar.addMenu("&File")
        for item in (self.act_add_files, self.act_add_folder, self.act_import_mendeley, None, self.act_new_folder,
                     None, self.act_open, self.act_show_in_folder, None, self.act_quit):  # fmt: skip
            file_menu.addSeparator() if item is None else file_menu.addAction(item)
        edit_menu = bar.addMenu("&Edit")
        for item in (self.act_copy_cite, self.act_copy_bibtex, None, self.act_select_all, self.act_command,
                     self.act_find, self.act_save_search, None, self.act_verify, None, self.act_delete):  # fmt: skip
            edit_menu.addSeparator() if item is None else edit_menu.addAction(item)
        view_menu = bar.addMenu("&View")
        view_menu.addAction(self.act_view_rows)
        view_menu.addAction(self.act_view_table)
        self.columns_menu = view_menu.addMenu("Table Columns")
        self.columns_menu.aboutToShow.connect(lambda: self._fill_columns_menu(self.columns_menu))
        view_menu.addSeparator()
        view_menu.addAction(self.act_show_filter)
        view_menu.addAction(self.act_show_details)
        view_menu.addSeparator()
        view_menu.addAction(self.act_study)
        view_menu.addAction(self.act_edit_settings)
        tools_menu = bar.addMenu("&Tools")
        for item in (self.act_sync, self.act_lookup, self.act_apply_naming, self.act_find_pdfs, None,
                     self.act_share, self.act_sync_devices, None,
                     self.act_open_library, self.act_open_bib_dir, self.act_open_inbox, None,
                     self.act_preferences):  # fmt: skip
            tools_menu.addSeparator() if item is None else tools_menu.addAction(item)
        help_menu = bar.addMenu("&Help")
        help_menu.addAction(self.act_help)

    def palette_actions(self) -> list[QAction]:
        """Actions offered by the command palette (search box text starting with ">")."""
        return [
            self.act_add_files, self.act_add_folder, self.act_import_mendeley, self.act_new_folder,
            self.act_copy_cite, self.act_copy_bibtex, self.act_open, self.act_show_in_folder, self.act_lookup,
            self.act_verify, self.act_study, self.act_attach_pdf, self.act_add_supplements, self.act_sync,
            self.act_find_pdfs, self.act_apply_naming, self.act_share, self.act_sync_devices, self.act_save_search,
            self.act_view_rows, self.act_view_table, self.act_edit_settings, self.act_open_library,
            self.act_open_bib_dir, self.act_open_inbox, self.act_preferences, self.act_help,
        ]  # fmt: skip

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

    def _create_top_bar(self) -> QWidget:
        bar = QWidget()
        bar.setObjectName("topBar")
        row = QHBoxLayout(bar)
        row.setContentsMargins(24, 12, 24, 12)
        row.setSpacing(12)
        self.breadcrumb = QLabel()
        self.breadcrumb.setObjectName("breadcrumb")
        self.breadcrumb.setTextFormat(Qt.TextFormat.RichText)
        row.addWidget(self.breadcrumb)
        row.addStretch(1)
        self.search = QLineEdit()
        self.search.setObjectName("searchBox")
        self.search.setMinimumWidth(360)
        self.search.setMaximumWidth(600)
        self.search.setClearButtonEnabled(True)
        row.addWidget(self.search, 3)
        kbd = QLabel("Ctrl K")
        kbd.setObjectName("kbd")
        kbd.setToolTip("Ctrl+K: search, or type > to run a command")
        row.addWidget(kbd)
        row.addStretch(1)
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
        # command palette: the search box with a leading ">"
        self.palette_model = QStringListModel(self)
        self.completer = QCompleter(self.palette_model, self)
        self.completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self.completer.setFilterMode(Qt.MatchFlag.MatchContains)
        self.completer.setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
        self.completer.setWidget(self.search)
        self.completer.activated.connect(self.run_command)
        add_menu = QMenu(self)
        add_menu.addAction(self.act_add_files)
        add_menu.addAction(self.act_add_folder)
        add_menu.addSeparator()
        add_menu.addAction(self.act_import_mendeley)
        self.import_button = QToolButton()
        self.import_button.setObjectName("primaryCommand")
        self.import_button.setDefaultAction(self.act_add_primary)
        self.import_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.import_button.setMenu(add_menu)
        self.import_button.setPopupMode(QToolButton.ToolButtonPopupMode.MenuButtonPopup)
        self.import_button.setIconSize(QSize(16, 16))
        row.addWidget(self.import_button)
        self.settings_button = QToolButton()
        self.settings_button.setObjectName("command")
        self.settings_button.setDefaultAction(self.act_preferences)
        self.settings_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        self.settings_button.setIconSize(QSize(18, 18))
        row.addWidget(self.settings_button)
        return bar

    def _create_panes(self) -> None:
        self.shelf = Shelf()
        self.tree: CollectionTree = self.shelf.tree
        self.tree.source.drop_handler = self._on_tree_drop
        self.device_label = self.shelf.device_label

        self.model = PaperTableModel(self)
        self.proxy = PaperFilterProxy(self)
        self.proxy.setSourceModel(self.model)
        self.table = DocumentTable(on_add=self.add_files_dialog)
        self.table.setModel(self.proxy)
        self.table.files_dropped = self._on_files_dropped
        self.table.addAction(self.act_open)
        self.table.addAction(self.act_delete)

        # header of the current view, with its buttons
        self.header = ViewHeader()
        self.list_title = self.header.title
        self.list_subtitle = self.header.subtitle
        self.open_bib_button = QPushButton("Open .bib")
        self.open_bib_button.clicked.connect(self.open_current_bib)
        self.edit_settings = QPushButton("Project settings")
        self.edit_settings.setToolTip("Table columns and, for a project, the second copy of its .bib file")
        self.edit_settings.clicked.connect(lambda: self.edit_view_settings())
        self.verify_all_button = QPushButton("Verify all with a DOI")
        self.verify_all_button.setToolTip("Mark every reference of the Inbox that was matched by its DOI as verified")
        self.verify_all_button.clicked.connect(self.verify_all_with_doi)
        self.edit_search_button = QPushButton("Edit search")
        self.edit_search_button.clicked.connect(self.edit_saved_search)
        for button in (self.verify_all_button, self.edit_search_button, self.open_bib_button, self.edit_settings):
            self.header.add_button(button)
        self.header.addMissingRequested.connect(self.add_missing_citations)
        self.header.showUnusedRequested.connect(self.show_uncited)
        self.header.recheckRequested.connect(lambda: self.check_manuscript(force=True))

        self.filters = FilterBar(self.model.papers, [(k, v[0]) for k, v in SORTS.items()])
        self.filters.changed.connect(self._on_filters_changed)
        self.filters.sortChanged.connect(self.set_sort)
        self.filters.viewChanged.connect(self.set_view_mode)
        self.filters.restrictCleared.connect(lambda: self._restrict(None))

        list_header = QWidget()
        list_header.setObjectName("listHeader")
        lh = QHBoxLayout(list_header)
        lh.setContentsMargins(16, 10, 16, 10)
        self.count_label = QLabel()
        self.count_label.setObjectName("listInfo")
        self.list_hint = QLabel("Bold title = unread")
        self.list_hint.setObjectName("listInfo")
        lh.addWidget(self.count_label)
        lh.addStretch(1)
        lh.addWidget(self.list_hint)

        self.inspector = ReferenceSheet()
        self.sheet = self.inspector
        self.main_split = QSplitter(Qt.Orientation.Horizontal)
        self.main_split.addWidget(self._card(list_header, self.table, margins=(1, 1, 1, 6)))
        self.main_split.addWidget(self._card(self.inspector, margins=(1, 1, 1, 1)))
        self.main_split.setStretchFactor(0, 1)
        self.main_split.setCollapsible(0, False)
        self.main_split.setSizes([820, 430])

        content = QWidget()
        content.setObjectName("content")
        cv = QVBoxLayout(content)
        cv.setContentsMargins(0, 0, 0, 0)
        cv.setSpacing(0)
        cv.addWidget(self._create_top_bar())
        body = QWidget()
        bv = QVBoxLayout(body)
        bv.setContentsMargins(24, 0, 24, 16)
        bv.setSpacing(0)
        bv.addWidget(self.header)
        bv.addWidget(self.filters)
        bv.addWidget(self.main_split, 1)
        cv.addWidget(body, 1)

        self.outer_split = QSplitter(Qt.Orientation.Horizontal)
        self.outer_split.setHandleWidth(1)
        self.outer_split.addWidget(self.shelf)
        self.outer_split.addWidget(content)
        self.outer_split.setStretchFactor(1, 1)
        self.outer_split.setCollapsible(1, False)
        self.outer_split.setSizes([268, 1212])
        workspace = QWidget()
        workspace.setObjectName("workspace")
        wl = QVBoxLayout(workspace)
        wl.setContentsMargins(0, 0, 0, 0)
        wl.addWidget(self.outer_split)

        self.study = StudyView()
        self.pages = QStackedWidget()
        self.pages.setObjectName("central")
        self.pages.addWidget(workspace)
        self.pages.addWidget(self.study)
        self.setCentralWidget(self.pages)
        self._apply_default_columns()

    def _create_statusbar(self) -> None:
        bar = self.statusBar()
        bar.setSizeGripEnabled(True)
        self.status_label = QLabel()
        bar.addWidget(self.status_label, 1)
        self.progress = QProgressBar()
        self.progress.setFixedWidth(160)
        self.progress.setTextVisible(False)
        self.progress.hide()
        bar.addPermanentWidget(self.progress)
        self._update_device_label()
        if self.library.sync is not None:
            self._device_timer.start()
            QTimer.singleShot(1500, self._device_sync_tick)

    def _wire(self) -> None:
        self.tree.nodeSelected.connect(self._on_node_selected)
        self.tree.folderRenamed.connect(self._rename_folder)
        self.tree.createFolderRequested.connect(self.new_folder)
        self.tree.customContextMenuRequested.connect(self._tree_menu)
        self.table.selectionModel().selectionChanged.connect(self._on_selection_changed)
        self.table.doubleClicked.connect(lambda _index: self.open_selected())
        self.table.clicked.connect(self._on_table_clicked)
        self.table.reference_delegate.starClicked.connect(self._toggle_star)
        self.table.customContextMenuRequested.connect(self._table_menu)
        self.table.horizontalHeader().customContextMenuRequested.connect(self._header_menu)
        self.table.horizontalHeader().sortIndicatorChanged.connect(self._on_header_sorted)
        sheet = self.inspector
        sheet.fieldEdited.connect(self._on_field_edited)
        sheet.notesEdited.connect(self._on_notes_edited)
        sheet.lookupRequested.connect(self.lookup_selected)
        sheet.openFileRequested.connect(self.open_selected)
        sheet.openUrlRequested.connect(self._open_url)
        sheet.revealFileRequested.connect(self.show_selected_in_folder)
        sheet.markReviewedRequested.connect(self._mark_reviewed)
        sheet.attachFileRequested.connect(self.attach_pdf_dialog)
        sheet.attachmentAction.connect(self._on_attachment_action)
        sheet.addSupplementsRequested.connect(self.add_supplements_dialog)
        sheet.projectToggled.connect(self._on_project_toggled)
        sheet.newProjectRequested.connect(self._new_project_for_selection)
        sheet.favoriteToggled.connect(lambda: self._toggle_star(sheet.paper_id) if sheet.paper_id else None)
        sheet.citeCopied.connect(self.notify)
        sheet.studyRequested.connect(self.enter_study)
        self.study.closeRequested.connect(self.exit_study)
        self.study.openPdfRequested.connect(self.open_selected)
        self.study.notesEdited.connect(self._on_notes_edited)
        self.search.textChanged.connect(self._on_search_text)
        self.search.returnPressed.connect(self._on_search_return)
        self.importer.progress.connect(self._on_import_progress)
        self.importer.imported.connect(self._on_imported)
        self.importer.failed.connect(self._on_import_failed)
        self.importer.batchFinished.connect(self._on_import_finished)
        self.mendeley.planned.connect(self._on_mendeley_planned)
        self.mendeley.progress.connect(self._on_import_progress)
        self.mendeley.finished.connect(self._on_mendeley_finished)
        self.inbox_bridge.fileReady.connect(self._on_inbox_file)
        self.study.assistant.bind(
            self.library,
            KeyStore(self.user_config.parent),
            save_settings=self._save_assistant_settings,
            launch=self._launch,
            reveal=self._reveal,
        )
        self.study.assistant.paperChanged.connect(self._on_assistant_notes)
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
        for visual, col in enumerate(DEFAULT_ORDER):
            current = header.visualIndex(int(col))
            if current != visual:
                header.moveSection(current, visual)
        for col in Col:
            header.resizeSection(int(col), DEFAULT_WIDTHS[col])
        self._table_hidden = {int(c) for c in DEFAULT_HIDDEN}
        self._apply_view_columns()

    def _fix_resize_modes(self) -> None:
        header = self.table.horizontalHeader()
        for col in ICON_COLUMNS:
            header.setSectionResizeMode(int(col), QHeaderView.ResizeMode.Fixed)
            header.resizeSection(int(col), DEFAULT_WIDTHS[col])
        if self.view_mode == VIEW_ROWS:
            header.setSectionResizeMode(int(Col.REFERENCE), QHeaderView.ResizeMode.Stretch)
            header.setSectionResizeMode(int(Col.TITLE), QHeaderView.ResizeMode.Interactive)
        else:
            header.setSectionResizeMode(int(Col.REFERENCE), QHeaderView.ResizeMode.Interactive)
            header.setSectionResizeMode(int(Col.TITLE), QHeaderView.ResizeMode.Stretch)

    def _apply_view_columns(self) -> None:
        """Rows view: the single painted column, no header. Table view: the user's columns."""
        header = self.table.horizontalHeader()
        rows = self.view_mode == VIEW_ROWS
        for col in Col:
            if rows:
                hidden = col != Col.REFERENCE
            else:
                hidden = col == Col.REFERENCE or int(col) in self._table_hidden
            header.setSectionHidden(int(col), hidden)
        header.setVisible(not rows)
        self.table.mode = self.view_mode
        self._fix_resize_modes()
        self.table.update_row_height()
        self.list_hint.setVisible(True)
        self.table.reference_delegate.year_gutter = rows and self.sort_key in ("year", "year_asc")
        self.table.viewport().update()

    def set_view_mode(self, mode: str) -> None:
        if mode not in (VIEW_ROWS, VIEW_TABLE):
            return
        if self.view_mode == VIEW_TABLE:  # remember the table's columns before hiding them
            header = self.table.horizontalHeader()
            self._table_hidden = {int(c) for c in TABLE_COLUMNS if header.isSectionHidden(int(c))}
        self.view_mode = mode
        self.act_view_rows.setChecked(mode == VIEW_ROWS)
        self.act_view_table.setChecked(mode == VIEW_TABLE)
        self.filters.set_view(mode)
        self._apply_view_columns()
        self.columns_menu.setEnabled(mode == VIEW_TABLE)

    def set_sort(self, key: str) -> None:
        if key not in SORTS:
            key = DEFAULT_SORT
        self.sort_key = key
        _label, column, order = SORTS[key]
        self.filters.set_sort(key)
        header = self.table.horizontalHeader()
        header.blockSignals(True)
        self.table.sortByColumn(int(column), order)
        header.blockSignals(False)
        self.table.reference_delegate.year_gutter = self.view_mode == VIEW_ROWS and key in ("year", "year_asc")
        self.table.viewport().update()

    def _on_header_sorted(self, column: int, order: Qt.SortOrder) -> None:
        """Table view: a click on a column header; keep the sort menu in step when it matches an entry."""
        for key, (_label, col, sort_order) in SORTS.items():
            if int(col) == column and sort_order == order:
                self.sort_key = key
                self.filters.set_sort(key)
                return
        self.filters.sort_button.setText("Sort: by column")

    def _restore_state(self) -> None:
        s = self.settings
        if (geometry := s.value("window/geometry")) is not None:
            self.restoreGeometry(geometry)
        if (state := s.value("window/split8")) is not None:
            self.main_split.restoreState(state)
        if (state := s.value("window/shelf8")) is not None:
            self.outer_split.restoreState(state)
        hidden = s.value("table/hidden8")
        if hidden is not None:
            values = hidden if isinstance(hidden, list) else str(hidden).split(",")
            self._table_hidden = {int(v) for v in values if str(v).strip().isdigit()}
        scope = str(s.value("search/scope", "all"))
        self.search_scope = scope if scope in SCOPES else "all"
        self.set_search_mode(str(s.value("search/mode", SEARCH_METADATA)))
        self.act_show_filter.setChecked(bool(s.value("view/filterBar", True, type=bool)))
        self.act_show_details.setChecked(bool(s.value("view/sheet", True, type=bool)))
        self._set_filter_visible(self.act_show_filter.isChecked())
        self.set_view_mode(str(s.value("view/mode", VIEW_ROWS)))
        self.set_sort(str(s.value("view/sort", DEFAULT_SORT)))

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
        s.setValue("window/split8", self.main_split.saveState())
        s.setValue("window/shelf8", self.outer_split.saveState())
        if self.view_mode == VIEW_TABLE:
            header = self.table.horizontalHeader()
            self._table_hidden = {int(c) for c in TABLE_COLUMNS if header.isSectionHidden(int(c))}
        s.setValue("table/hidden8", ",".join(str(c) for c in sorted(self._table_hidden)))
        s.setValue("search/mode", self.search_mode)
        s.setValue("search/scope", self.search_scope)
        s.setValue("view/filterBar", self.act_show_filter.isChecked())
        s.setValue("view/sheet", self.act_show_details.isChecked())
        s.setValue("view/mode", self.view_mode)
        s.setValue("view/sort", self.sort_key)
        kind, cid = self.node
        s.setValue("view/node", f"{kind}:{cid}" if cid is not None else str(kind))
        s.sync()

    def closeEvent(self, event: QCloseEvent) -> None:
        focus = QApplication.focusWidget()
        if focus is not None:
            focus.clearFocus()  # commits an inline edit in progress
        self.inspector.flush_notes()
        self.study.flush_notes()
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
    def _bib_statuses(self) -> dict[int, tuple[str, str]]:
        """The line under each project on the shelf: its .bib file and when it was last written."""
        statuses: dict[int, tuple[str, str]] = {}
        today = date.today()
        for collection in self.library.collections():
            path = self.library.bib.collection_path(collection.slug)
            try:
                stamp = datetime.fromtimestamp(path.stat().st_mtime)
            except OSError:
                statuses[collection.id] = (".bib not written yet", "pending")
                continue
            when = f"{stamp:%H:%M}" if stamp.date() == today else f"{stamp.day} {stamp:%b}"
            statuses[collection.id] = (f".bib written {when}", "ok")
        return statuses

    def reload_tree(self, select: Node | None = None) -> None:
        self.tree.rebuild(
            self.library.collections(),
            select=select,
            statuses=self._bib_statuses(),
            saved=self.library.config.saved_searches,
        )
        self.node = self.tree.current_node() or (NodeKind.ALL, None)
        self.reload_view(keep_selection=False)
        if self.node[0] == NodeKind.FOLDER:
            self.check_manuscript()

    def _papers_for(self, node: Node) -> list[Paper]:
        kind, cid = node
        lib = self.library
        if kind == NodeKind.FOLDER and cid is not None:
            return lib.papers(cid)
        if kind == NodeKind.INBOX:
            return lib.papers_needing_review()
        return lib.papers()

    def reload_view(self, keep_selection: bool = True) -> None:
        selected = self.selected_ids() if keep_selection else []
        papers = self._papers_for(self.node)
        self.model.set_papers(papers, self.library.folder_names_by_paper())
        saved = self.tree.saved_search(self.node[1]) if self.node[0] == NodeKind.SAVED else None
        self.proxy.set_saved_query(parse_query(saved[1]) if saved else Query())
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
        self._restrict(None)
        if self.node[0] == NodeKind.SAVED:
            saved = self.tree.saved_search(collection_id)
            if saved and saved[2]:
                self.set_sort(saved[2])
        self.reload_view(keep_selection=False)
        self.table.scrollToTop()
        if self.node[0] == NodeKind.FOLDER:
            self.check_manuscript()

    def current_title(self) -> str:
        kind, cid = self.node
        if kind == NodeKind.FOLDER and cid is not None:
            return self.tree.collection_name(cid)
        if kind == NodeKind.SAVED:
            saved = self.tree.saved_search(cid)
            return saved[0] if saved else "Saved search"
        return NODE_TITLES.get(kind, "Library")

    def _update_header(self) -> None:
        kind, cid = self.node
        title = self.current_title()
        self.list_title.setText(title)
        parent = {NodeKind.FOLDER: "Projects", NodeKind.SAVED: "Saved searches"}.get(kind)
        self.breadcrumb.setText(
            f"{parent} <span style='opacity:0.6'>/</span> <b>{title}</b>" if parent else f"<b>{title}</b>"
        )
        total = self.model.rowCount()
        refs = f"{total:,} reference{'s' if total != 1 else ''}"
        collection = self.library.db.get_collection(cid) if kind == NodeKind.FOLDER and cid is not None else None
        t = theme.current()
        if collection is not None:
            path = self.library.bib.collection_path(collection.slug)
            status = self._bib_statuses().get(collection.id, (".bib not written yet", "pending"))
            color = t.ok if status[1] == "ok" else t.warning
            line = f"{refs} · <span style='color:{color}'>{path.name} {status[0].removeprefix('.bib ')}</span>"
            if collection.bib_export_path:
                line += f" · also written to <code>{_display_path(collection.bib_export_path)}</code>"
            self.list_subtitle.setText(line)
        elif kind == NodeKind.INBOX:
            self.list_subtitle.setText(
                f"{refs} to verify. Check the details of each one, then mark it as verified (Ctrl+Return)."
            )
        elif kind == NodeKind.SAVED:
            saved = self.tree.saved_search(cid)
            query = saved[1] if saved else ""
            self.list_subtitle.setText(f"{refs} · <code>{query or 'every reference'}</code>")
        else:
            master = self.library.bib.bibtex_dir / "master.bib"
            self.list_subtitle.setText(f"{refs} · every reference is written to {master.name}")
        self.open_bib_button.setVisible(collection is not None)
        self.edit_settings.setText("Project settings" if collection is not None else "View settings")
        self.verify_all_button.setVisible(kind == NodeKind.INBOX)
        self.edit_search_button.setVisible(kind == NodeKind.SAVED)
        if collection is None:
            self.manuscript = None
            self.header.show_check(None)
        self.list_hint.setText("Bold title = unread" if kind != NodeKind.INBOX else "Ctrl+Return = mark as verified")

    def _update_count(self, *_args: Any) -> None:
        shown, total = self.proxy.rowCount(), self.model.rowCount()
        noun = "reference" if total == 1 else "references"
        self.count_label.setText(f"{shown:,} of {total:,} {noun}" if shown != total else f"{total:,} {noun}")
        self.table.update_empty_state(empty=shown == 0, filtered=total > 0, inbox=self.node[0] == NodeKind.INBOX)

    def _refresh_counts(self) -> None:
        self.tree.update_counts(self.library.counts())

    # ------------------------------------------------------------------ manuscript check
    def check_manuscript(self, *, force: bool = False) -> None:
        """Projects that also write their .bib next to a manuscript: compare the keys its .tex files cite."""
        kind, cid = self.node
        collection = self.library.db.get_collection(cid) if kind == NodeKind.FOLDER and cid is not None else None
        folder = manuscript_folder(collection.bib_export_path) if collection is not None else None
        if folder is None:
            self.manuscript = None
            self.header.show_check(None)
            return
        self.runner.submit(
            scan_citations,
            folder,
            on_done=lambda scan, c=cid: self._on_manuscript_scanned(c, scan),
            on_error=lambda message: log.warning("Manuscript check failed: %s", message),
        )
        if force:
            self.notify(f"Checking the citations in {folder}...")

    def _on_manuscript_scanned(self, collection_id: int, scan: CitationScan) -> None:
        if self.node != (NodeKind.FOLDER, collection_id):
            return
        project = [p.citation_key for p in self.library.papers(collection_id)]
        library = [row[0] for row in self.library.db.conn.execute("SELECT citation_key FROM papers")]
        self.manuscript = compare(scan, project, library)
        main = next((f.name for f in scan.tex_files if f.name.lower() == "main.tex"), "")
        self.header.show_check(self.manuscript, main)

    def add_missing_citations(self) -> None:
        check, (kind, cid) = self.manuscript, self.node
        if check is None or kind != NodeKind.FOLDER or cid is None or not check.missing_in_library:
            return
        added = 0
        for key in check.missing_in_library:
            paper = self.library.db.get_paper_by_citation_key(key)
            if paper is not None:
                added += self.library.add_to_collection(paper.id, cid)
        self.notify(f"Added {added} cited reference(s) to '{self.current_title()}'")
        self.reload_view(keep_selection=True)
        self.check_manuscript()

    def show_uncited(self) -> None:
        check = self.manuscript
        if check is None or not check.unused:
            return
        unused = {k.casefold() for k in check.unused}
        ids = {p.id for p in self.model.papers() if p.citation_key.casefold() in unused}
        self._restrict(ids, f"Never cited ({len(ids)})")

    def _restrict(self, ids: set[int] | None, label: str | None = None) -> None:
        self.proxy.set_restrict(ids)
        self.filters.show_restrict(label if ids is not None else None)
        self._update_count()

    # ------------------------------------------------------------------ search, chips, saved searches
    def set_search_mode(self, mode: str) -> None:
        self.search_mode = mode if mode in (SEARCH_METADATA, SEARCH_FULLTEXT) else SEARCH_METADATA
        fulltext = self.search_mode == SEARCH_FULLTEXT
        self.act_search_full.setChecked(fulltext)
        self.scope_actions[self.search_scope].setChecked(not fulltext)
        if fulltext:
            self.search.setPlaceholderText("Search the full text of the PDFs...   (author:, year: also work)")
        elif self.search_scope == "all":
            self.search.setPlaceholderText("Search references, or type > for a command")
        else:
            self.search.setPlaceholderText(f"Search {SCOPE_LABELS[self.search_scope].lower()}...")
        what = "full text" if fulltext else SCOPE_LABELS[self.search_scope]
        self.search_mode_action.setToolTip(f"Searching: {what}. Click to choose what to search.")
        self.search.setToolTip(
            "Words are searched in the field chosen with the magnifier; qualifiers work everywhere:\n"
            + "\n".join(f"  {example}   {meaning}" for example, meaning in SYNTAX_HELP)
            + "\nType > to run a command."
        )
        self._apply_search()

    def set_search_scope(self, scope: str) -> None:
        """Field searched by plain words; qualifiers such as author: always work."""
        self.search_scope = scope if scope in SCOPES else "all"
        self.set_search_mode(SEARCH_METADATA)

    def show_search_help(self) -> None:
        rows = "".join(f"<tr><td><code>{e}</code></td><td>&nbsp;&nbsp;{m}</td></tr>" for e, m in SYNTAX_HELP)
        box = QMessageBox(self)
        box.setWindowTitle("Search syntax")
        box.setTextFormat(Qt.TextFormat.RichText)
        box.setText(
            "<p>Words are searched in the field chosen from the magnifier menu (all fields by default). "
            "Terms are combined with AND; case and accents are ignored; use quotes for phrases. "
            "Type <code>&gt;</code> first to run a command instead.</p>"
            f"<table>{rows}</table>"
        )
        box.exec()

    def focus_search(self) -> None:
        if self.pages.currentIndex() != 0:
            self.exit_study()
        self.search.setFocus()
        self.search.selectAll()

    def _on_search_text(self, text: str) -> None:
        if text.startswith(">"):
            self.palette_model.setStringList([_plain(a.text()) for a in self.palette_actions() if a.isEnabled()])
            self.completer.setCompletionPrefix(text[1:].strip())
            self.completer.complete()
            return
        self._search_timer.start()

    def _on_search_return(self) -> None:
        text = self.search.text()
        if text.startswith(">"):
            match = self.completer.currentCompletion() or text[1:].strip()
            self.run_command(match)

    def run_command(self, name: str) -> bool:
        """Command palette: run the action whose name is ``name`` (case-insensitive, accelerators ignored)."""
        wanted = _plain(name).casefold()
        for action in self.palette_actions():
            if _plain(action.text()).casefold() == wanted and action.isEnabled():
                self.search.clear()
                self.completer.popup().hide()
                action.trigger()
                return True
        self.notify(f"No command named “{name}”")
        return False

    def _apply_search(self) -> None:
        text = self.search.text().strip()
        if text.startswith(">"):
            text = ""
        query = parse_query(text)
        if self.search_mode == SEARCH_FULLTEXT and query.free_text:
            hits = self.library.search_fulltext(query.free_text, limit=5000)
            self.proxy.set_ids({hit.paper_id for hit in hits})
            self.proxy.set_query(query, "all", skip_free_text=True)  # qualifiers still filter the hits
        else:
            self.proxy.set_ids(None)
            self.proxy.set_query(query, self.search_scope)
        self._update_count()

    def _on_filters_changed(self) -> None:
        self.proxy.set_chip_query(self.filters.chip_query())
        self.proxy.set_facets(self.filters.facets())
        self._update_count()

    def _on_facet_changed(self, kind: str | None, value: str | None) -> None:
        if kind:
            self.filters.set_facet(kind, value)

    def save_current_search(self) -> None:
        parts = [self.search.text().strip()] if not self.search.text().startswith(">") else []
        parts += [term for term, chip in self.filters.toggles.items() if chip.isChecked()]
        if self.filters._year is not None:
            parts.append(self.filters._year[1])
        query = " ".join(p for p in parts if p)
        name, ok = QInputDialog.getText(self, "Save Search", f"Name for the search  {query or '(every reference)'}:")
        if not ok or not name.strip():
            return
        config = self.library.config
        config.saved_searches = (*config.saved_searches, (name.strip(), query, self.sort_key))
        self._save_user_settings()
        self.reload_tree(select=(NodeKind.SAVED, len(config.saved_searches) - 1))
        self.notify(f"Saved search '{name.strip()}'")

    def edit_saved_search(self, position: int | None = None) -> None:
        position = position if position is not None else self.node[1]
        saved = self.tree.saved_search(position)
        if saved is None or position is None:
            return
        query, ok = QInputDialog.getText(self, "Edit Search", f"Query of '{saved[0]}':", text=saved[1])
        if not ok:
            return
        searches = list(self.library.config.saved_searches)
        searches[position] = (saved[0], query.strip(), saved[2])
        self.library.config.saved_searches = tuple(searches)
        self._save_user_settings()
        self.reload_tree(select=(NodeKind.SAVED, position))

    def remove_saved_search(self, position: int) -> None:
        searches = list(self.library.config.saved_searches)
        if 0 <= position < len(searches):
            name = searches.pop(position)[0]
            self.library.config.saved_searches = tuple(searches)
            self._save_user_settings()
            self.reload_tree(select=(NodeKind.ALL, None))
            self.notify(f"Removed the saved search '{name}'")

    def _save_user_settings(self) -> None:
        try:
            self.library.config.save_user_settings(self.user_config)
        except OSError as exc:
            log.warning("Could not save the settings: %s", exc)

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

    def _select_row(self, row: int) -> None:
        if self.proxy.rowCount() == 0:
            self.inspector.clear()
            return
        row = max(0, min(row, self.proxy.rowCount() - 1))
        pid = self.proxy.index(row, 0).data(PAPER_ID_ROLE)
        if pid is not None:
            self.select_ids([int(pid)])

    def _on_selection_changed(self, *_args: Any) -> None:
        papers = self.selected_papers()
        if len(papers) == 1:
            self._show_paper(papers[0])
        elif not papers:
            self.inspector.clear()
        else:
            self.inspector.clear(f"{len(papers)} references selected")
        self.act_study.setEnabled(len(papers) == 1)

    def _project_entries(self) -> list[tuple[int, str, int]]:
        return [(c.id, c.name, depth) for c, depth in self._folder_entries()]

    def _show_paper(self, paper: Paper) -> None:
        pdf = self.library.pdf_path(paper)
        name = pdf.name if pdf is not None else ""
        attachments = self.library.attachments(paper.id) if paper.attachments else []
        available = {a.id: self.library.attachment_available(a) for a in attachments}
        kind, cid = self.node
        bib_name = ""
        if kind == NodeKind.FOLDER and cid is not None and (collection := self.library.db.get_collection(cid)):
            bib_name = f"{collection.slug}.bib"
        self.inspector.set_paper(
            paper,
            [(c.id, c.name) for c in self.library.collections_of(paper.id)],
            name,
            attachments,
            available,
            bibtex=self.library.bibtex_entry(paper),
            bib_name=bib_name,
            all_projects=self._project_entries(),
        )
        if self.pages.currentIndex() == 1 and self.study.paper is not None and self.study.paper.id == paper.id:
            self.study.set_paper(paper)
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

    # ================================================================== study mode
    def enter_study(self) -> None:
        papers = self.selected_papers()
        if len(papers) != 1:
            self.notify("Select one reference to study")
            return
        self.inspector.flush_notes()
        self.study.set_context(self.current_title())
        self.study.set_paper(papers[0])
        self.pages.setCurrentIndex(1)
        self.study.editor.setFocus()

    def exit_study(self) -> None:
        self.study.flush_notes()
        self.pages.setCurrentIndex(0)
        pid = self.study.paper.id if self.study.paper is not None else None
        if pid is not None:
            try:
                paper = self.library.get_paper(pid)
            except KeyError:
                return
            self.model.replace_paper(paper)
            if self.inspector.paper_id == pid:
                self.inspector.reload_notes(paper.notes)
        self.table.setFocus()

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

    def _toggle_star(self, paper_id: int | None) -> None:
        if paper_id is None:
            return
        row = self.model.row_of(paper_id)
        paper = self.model.paper(row) if row is not None else self.library.get_paper(paper_id)
        self._apply_flags([paper_id], favorite=not paper.favorite)

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
            "Delete References",
            f"Delete {len(papers)} reference(s) from the library?\n\n{keys}\n\n"
            f"{files}, and the entries are removed from every project and .bib file.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        for paper in papers:
            self.library.delete_paper(paper.id)
        self.reload_view(keep_selection=False)
        self.notify(f"Deleted {len(papers)} reference(s)")

    def add_to_folder(self, ids: Iterable[int], collection_id: int) -> None:
        added = sum(self.library.add_to_collection(pid, collection_id) for pid in ids)
        self.notify(f"Added {added} reference(s) to '{self.tree.collection_name(collection_id)}'")
        self._schedule_reload()

    def _on_project_toggled(self, collection_id: int, add: bool) -> None:
        pid = self.inspector.paper_id
        if pid is None:
            return
        name = self.tree.collection_name(collection_id)
        if add:
            self.library.add_to_collection(pid, collection_id)
            self.notify(f"Added to '{name}'")
        else:
            self.library.remove_from_collection(pid, collection_id)
            self.notify(f"Removed from '{name}' (the PDF stays in the library)")
        if self.node == (NodeKind.FOLDER, collection_id):
            self.reload_view(keep_selection=True)
        else:
            self.model.set_folders(self.library.folder_names_by_paper())
            self._refresh_paper(self.library.get_paper(pid))
            self._refresh_counts()
        self.tree.update_statuses(self._bib_statuses())

    def _new_project_for_selection(self) -> None:
        ids = self.selected_ids()
        collection = self.new_folder(select=False)
        if collection is not None and ids:
            self.add_to_folder(ids, collection.id)

    def remove_from_current_folder(self) -> None:
        kind, cid = self.node
        if kind != NodeKind.FOLDER or cid is None:
            return
        papers = self.selected_papers()
        for paper in papers:
            self.library.remove_from_collection(paper.id, cid)
        self.reload_view(keep_selection=False)
        self.notify(f"Removed {len(papers)} reference(s) from '{self.tree.collection_name(cid)}' (PDFs kept)")

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
            QMessageBox.warning(self, "Cannot update the reference", str(exc))
            paper = self.library.get_paper(pid)
        self._refresh_paper(paper)
        if paper.pdf_managed and before.pdf_managed and paper.relative_pdf_path != before.relative_pdf_path:
            self.notify(f"PDF renamed: {before.relative_pdf_path} → {paper.relative_pdf_path}")

    def _on_notes_edited(self, paper_id: int, text: str) -> None:
        try:
            paper = self.library.update_flags(paper_id, notes=text)
        except KeyError:  # deleted meanwhile
            return
        self.model.replace_paper(paper)
        if self.inspector.paper_id == paper_id:
            self.inspector.reload_notes(paper.notes)
        if self.study.paper is not None and self.study.paper.id == paper_id:
            self.study.paper = paper
            self.study.reload_notes(paper.notes)

    def _mark_reviewed(self) -> None:
        pid = self.inspector.paper_id
        if pid is not None:
            self._mark_verified([pid])

    def mark_selected_verified(self) -> None:
        ids = [p.id for p in self.selected_papers() if p.needs_review]
        if ids:
            self._mark_verified(ids)

    def _mark_verified(self, ids: list[int]) -> None:
        """Take references out of the Inbox; in the Inbox the next reference is selected (triage)."""
        row = min((r for pid in ids if (r := self._proxy_row(pid)) is not None), default=0)
        for pid in ids:
            self._refresh_paper(self.library.update_metadata(pid, needs_review=False))
        self._refresh_counts()
        if self.node[0] == NodeKind.INBOX:
            self.reload_view(keep_selection=False)
            self._select_row(row)
        self.notify(f"{len(ids)} reference{'s' if len(ids) != 1 else ''} verified")

    def _proxy_row(self, paper_id: int) -> int | None:
        row = self.model.row_of(paper_id)
        if row is None:
            return None
        index = self.proxy.mapFromSource(self.model.index(row, 0))
        return index.row() if index.isValid() else None

    def verify_all_with_doi(self) -> None:
        ids = [p.id for p in self.library.papers_needing_review() if p.meta.doi]
        if not ids:
            self.notify("No reference in the Inbox was matched by a DOI")
            return
        answer = QMessageBox.question(
            self, "Verify all with a DOI", f"Mark the {len(ids)} reference(s) matched by their DOI as verified?"
        )
        if answer == QMessageBox.StandardButton.Yes:
            self._mark_verified(ids)

    def lookup_selected(self, kind: str = "auto") -> None:
        papers = self.selected_papers()
        if len(papers) != 1:
            self.notify("Select a single reference to look up its metadata")
            return
        paper = papers[0]
        doi = paper.meta.doi if kind in ("auto", "doi") else ""
        arxiv_id = paper.meta.arxiv_id if kind in ("auto", "arxiv") else ""
        title = paper.meta.title if kind == "auto" else ""
        if not (doi or arxiv_id or title):
            self.notify("Enter a DOI, an arXiv ID or a title first")
            return
        self.notify("Looking up the metadata online...")
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
            QMessageBox.warning(self, "Cannot apply the metadata", str(exc))
            return
        self._refresh_paper(paper)
        self.notify(f"Metadata updated from {source}: {paper.citation_key}")

    # ================================================================== projects
    def _target_folder(self) -> int | None:
        return self.node[1] if self.node[0] == NodeKind.FOLDER else None

    def new_folder(self, parent_id: int | None = None, *, select: bool = True) -> Collection | None:
        title = "New Sub-project" if parent_id else "New Project"
        name, ok = QInputDialog.getText(self, title, "Project name (its .bib file is named after it):")
        if not ok or not name.strip():
            return None
        try:
            collection = self.library.create_collection(name, parent_id=parent_id)
        except (ValueError, KeyError) as exc:
            QMessageBox.warning(self, title, str(exc))
            return None
        self.reload_tree(select=(NodeKind.FOLDER, collection.id) if select else self.node)
        self.notify(f"Created project '{collection.name}' → bibtex/{collection.slug}.bib")
        return collection

    def remove_folder(self, collection_id: int | None = None) -> None:
        cid = collection_id if collection_id is not None else self._target_folder()
        if cid is None:
            self.notify("Select a project to remove")
            return
        name = self.tree.collection_name(cid)
        answer = QMessageBox.question(
            self,
            "Remove Project",
            f"Remove the project '{name}' and its sub-projects?\n\n"
            "References stay in your library and their PDFs stay on disk; only the project and its .bib file "
            "inside the library are removed.",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.library.delete_collection(cid)
        self.reload_tree(select=(NodeKind.ALL, None))
        self.notify(f"Removed project '{name}'")

    def _rename_folder(self, collection_id: int, name: str) -> None:
        try:
            collection = self.library.rename_collection(collection_id, name)
            self.notify(f"Renamed project → bibtex/{collection.slug}.bib")
        except (ValueError, KeyError) as exc:
            QMessageBox.warning(self, "Rename Project", str(exc))
        QTimer.singleShot(0, lambda: self.reload_tree(select=(NodeKind.FOLDER, collection_id)))

    def sync_bibs(self) -> None:
        self.library.sync_all_bibs()
        count = len(self.library.collections())
        self.tree.update_statuses(self._bib_statuses())
        self._update_header()
        self.notify(f"Rewrote {count} project .bib file(s) and master.bib")

    def open_current_bib(self) -> None:
        kind, cid = self.node
        collection = self.library.db.get_collection(cid) if kind == NodeKind.FOLDER and cid is not None else None
        if collection is not None:
            path = self.library.bib.collection_path(collection.slug)
            if not path.exists():
                self.library.sync_all_bibs()
            self._launch(QUrl.fromLocalFile(str(path)))

    def edit_view_settings(self, node: Node | None = None) -> None:
        kind, cid = node or self.node
        collection = self.library.db.get_collection(cid) if kind == NodeKind.FOLDER and cid is not None else None
        header = self.table.horizontalHeader()
        if self.view_mode == VIEW_TABLE:
            visible = {int(c) for c in TABLE_COLUMNS if not header.isSectionHidden(int(c))}
        else:
            visible = {int(c) for c in TABLE_COLUMNS if int(c) not in self._table_hidden}
        dialog = ViewSettingsDialog(
            self,
            title=collection.name if collection else self.current_title(),
            visible=visible,
            collection=collection,
            internal_bib=self.library.bib.collection_path(collection.slug) if collection else None,
        )
        if dialog.exec() != ViewSettingsDialog.DialogCode.Accepted:
            return
        self._table_hidden = {int(c) for c in TABLE_COLUMNS if int(c) not in dialog.visible_columns()}
        self._apply_view_columns()
        if collection is not None and dialog.external_path() != (collection.bib_export_path or None):
            self.set_export_path(collection, dialog.external_path())
            self._update_header()
            self.check_manuscript()

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
        self.notify(f"Importing {len(pdfs)} PDF(s)...")
        return len(pdfs)

    def _on_import_progress(self, done: int, total: int) -> None:
        self.progress.setRange(0, max(total, 1))
        self.progress.setValue(done)
        self.progress.setVisible(done < total)

    def _on_imported(self, result: IngestResult) -> None:
        key = result.paper.citation_key
        if result.status is IngestStatus.IMPORTED:
            self.notify(f"Imported {key}" + (" (to verify, in the Inbox)" if result.paper.needs_review else ""))
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
        if self.inspector.paper_id == paper.id:
            self.inspector.reload_notes(paper.notes)
        if self.study.paper is not None and self.study.paper.id == paper.id:
            self.study.paper = paper
            self.study.reload_notes(paper.notes, force=True)
        self.notify(f"Answer saved in the notes of {paper.citation_key}")

    def find_missing_pdfs(self) -> None:
        if self.attacher.running:
            self.notify("Another attach operation is still running")
            return
        missing = len(self.library.papers_without_pdf())
        if not missing:
            self.notify("Every reference has a PDF")
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
        menu.setDefaultAction(self._menu_action(menu, "Open PDF", self.open_selected))
        self._menu_action(menu, "Show PDF in Folder", self.show_selected_in_folder)
        menu.addSeparator()
        menu.addAction(self.act_copy_cite)
        menu.addAction(self.act_copy_bibtex)
        menu.addSeparator()
        mark = menu.addMenu("Mark")
        self._menu_action(mark, "Read", lambda: self._apply_flags(ids, is_read=True))
        self._menu_action(mark, "Unread", lambda: self._apply_flags(ids, is_read=False))
        mark.addSeparator()
        starred = not all(p.favorite for p in papers)
        self._menu_action(mark, "Starred" if starred else "Not Starred",
                          lambda: self._apply_flags(ids, favorite=starred), icons.star(True))  # fmt: skip
        mine = not all(p.my_publication for p in papers)
        self._menu_action(mark, "Authored by Me" if mine else "Not Authored by Me",
                          lambda: self._apply_flags(ids, my_publication=mine), icons.person())  # fmt: skip
        if any(p.needs_review for p in papers):
            mark.addSeparator()
            mark.addAction(self.act_verify)
        projects = menu.addMenu(icons.project(on_shelf=False), "Add to Project")
        entries = self._folder_entries()
        if not entries:
            projects.addAction("(no projects yet)").setEnabled(False)
        for collection, depth in entries:
            self._menu_action(projects, "    " * depth + collection.name,
                              partial(self.add_to_folder, ids, collection.id))  # fmt: skip
        projects.addSeparator()
        self._menu_action(projects, "New Project...", self._new_project_for_selection)
        if len(papers) == 1:
            self._fill_search_menu(menu.addMenu(icons.search(), "Find Similar"), papers[0])
        if self.node[0] == NodeKind.FOLDER:
            self._menu_action(menu, "Remove from This Project", self.remove_from_current_folder)
        menu.addSeparator()
        if len(papers) == 1:
            menu.addAction(self.act_study)
        lookup = self._menu_action(menu, "Look Up Metadata", lambda: self.lookup_selected("auto"))
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
            entries.append((f"Venue: {paper.meta.journal}", f"journal:{quoted(paper.meta.journal)}"))
        if paper.meta.year is not None:
            entries.append((f"Year: {paper.meta.year}", f"year:{paper.meta.year}"))
        for tag in split_list_field(paper.tags)[:6]:
            entries.append((f"Tag: {tag}", f"tag:{quoted(tag)}"))
        for label, query in entries:
            self._menu_action(menu, label, partial(self.search_for, query))
        if not entries:
            menu.addAction("(no author, journal or year)").setEnabled(False)

    def search_for(self, query: str) -> None:
        """Show every reference matching ``query`` (e.g. ``author:anand``), across the whole library."""
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
            self._menu_action(menu, "New Sub-project...", lambda: self.new_folder(cid), icons.folder_add())
            self._menu_action(menu, "Rename Project",
                              lambda: self.tree.edit(self.tree.find_index(NodeKind.FOLDER, cid)))  # fmt: skip
            self._menu_action(menu, "Remove Project...", lambda: self.remove_folder(cid), icons.folder_remove())
            menu.addSeparator()
            self._menu_action(menu, "Project Settings...", lambda: self.edit_view_settings(node))
            if collection is not None:
                bib = self.library.bib.collection_path(collection.slug)
                self._menu_action(menu, f"Open {bib.name}", lambda: self._launch(QUrl.fromLocalFile(str(bib))))
                self._menu_action(menu, f"Show {bib.name} in Folder", lambda: self._reveal(bib))
        elif node is not None and node[0] == NodeKind.SAVED and node[1] is not None:
            position = node[1]
            self._menu_action(menu, "Edit Search...", lambda: self.edit_saved_search(position))
            self._menu_action(menu, "Remove Saved Search", lambda: self.remove_saved_search(position))
        else:
            self._menu_action(menu, "New Project...", self.new_folder, icons.folder_add())
            menu.addAction(self.act_save_search)
            if node is not None and node[0] == NodeKind.ALL:
                self._menu_action(menu, "Rewrite .bib Files", self.sync_bibs, icons.sync())
            if node is not None and node[0] == NodeKind.INBOX:
                menu.addAction(self.act_open_inbox)
        menu.exec(self.tree.viewport().mapToGlobal(pos))

    def _fill_columns_menu(self, menu: QMenu) -> None:
        menu.clear()
        for col in TABLE_COLUMNS:
            action = menu.addAction(COLUMN_NAMES[col])
            action.setCheckable(True)
            action.setChecked(int(col) not in self._table_hidden)
            action.setEnabled(col != Col.TITLE)
            action.toggled.connect(partial(self._set_column_visible, int(col)))

    def _set_column_visible(self, column: int, visible: bool) -> None:
        if visible:
            self._table_hidden.discard(column)
        else:
            self._table_hidden.add(column)
        if self.view_mode == VIEW_TABLE:
            self.table.horizontalHeader().setSectionHidden(column, not visible)

    def _header_menu(self, pos: QPoint) -> None:
        menu = QMenu(self)
        self._fill_columns_menu(menu)
        menu.exec(self.table.horizontalHeader().mapToGlobal(pos))

    # ================================================================== misc
    def _set_filter_visible(self, visible: bool) -> None:
        self.filters.setVisible(visible)
        if not visible and self.filters.active():
            self.filters.clear()

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
            (self.act_study, icons.study(on_strong=False)),
            (self.act_verify, icons.check()),
        ):
            action.setIcon(icon)
        self.search_mode_action.setIcon(icons.search())
        self.table.empty_state.refresh()
        self.table.update_row_height()
        self.inspector.refresh_icons()
        self.study.refresh_icons()
        self.shelf.refresh()
        theme.style_window_frame(self, current)
        if rebuild:
            self.reload_tree(select=self.node)
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
            self.shelf.set_library(self.library.root, shared=False, status="")
            return
        others = max(0, len(engine.devices()) - 1)
        when = datetime.fromtimestamp(engine.last_sync).strftime("%H:%M") if engine.last_sync else "\u2014"
        status = f"Shared \u00b7 {others} other device{'' if others == 1 else 's'} \u00b7 synced {when}"
        self.shelf.set_library(self.library.root, shared=True, status=status)

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
        self.study.flush_notes()
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
        self.study.flush_notes()
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
