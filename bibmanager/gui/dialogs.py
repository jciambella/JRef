"""Dialogs: view settings, Preferences / first-run welcome, Mendeley import, naming rule, ZIP export, About."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from html import escape
from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QRadioButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .. import APP_AUTHOR, APP_AUTHOR_EMAIL, APP_NAME, APP_SUBTITLE, __version__
from ..core.config import DEFAULT_CONTACT_EMAIL, AppConfig
from ..core.export import ZipOptions, ZipPlan, ZipReport, format_size
from ..core.mendeley import (
    BibFileSummary,
    MendeleyImportOptions,
    MendeleyImportReport,
    default_mendeley_pdf_folder,
    find_mendeley_databases,
    suggest_bib_folder,
    summarize_bib_file,
)
from ..core.models import Collection, Paper
from . import icons
from .paper_model import COLUMN_NAMES, Col
from .workers import AttachReport


def _buttons(accept_text: str) -> QDialogButtonBox:
    """OK/Cancel box whose accept button carries the accent style (object name set before polishing)."""
    box = QDialogButtonBox()
    ok = QPushButton(accept_text)
    ok.setObjectName("primary")
    ok.setDefault(True)
    box.addButton(ok, QDialogButtonBox.ButtonRole.AcceptRole)
    box.addButton(QDialogButtonBox.StandardButton.Cancel)
    return box


class ViewSettingsDialog(QDialog):
    """Project settings: the columns of the table view and, for a project, its second ``.bib`` copy."""

    def __init__(
        self,
        parent: QWidget | None,
        *,
        title: str,
        visible: set[int],
        collection: Collection | None = None,
        internal_bib: Path | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Settings — {title}")
        self.setMinimumWidth(520)
        layout = QVBoxLayout(self)

        columns = QGroupBox("Columns of the table view")
        grid = QGridLayout(columns)
        self.checks: dict[int, QCheckBox] = {}
        for i, col in enumerate(Col):
            box = QCheckBox(COLUMN_NAMES[col])
            box.setChecked(int(col) in visible)
            box.setEnabled(col != Col.TITLE)
            grid.addWidget(box, i // 3, i % 3)
            self.checks[int(col)] = box
        layout.addWidget(columns)

        self.external = QLineEdit()
        if collection is not None:
            bib = QGroupBox("BibTeX export")
            v = QVBoxLayout(bib)
            v.addWidget(QLabel("Library copy (always kept in sync):"))
            row = QHBoxLayout()
            path_label = QLabel(str(internal_bib) if internal_bib else "")
            path_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            path_label.setWordWrap(True)
            row.addWidget(path_label, 1)
            show = QPushButton("Open folder")
            show.clicked.connect(
                lambda: internal_bib and QDesktopServices.openUrl(QUrl.fromLocalFile(str(internal_bib.parent)))
            )
            row.addWidget(show)
            v.addLayout(row)
            v.addWidget(QLabel("Also write to (e.g. references.bib inside your LaTeX manuscript folder):"))
            row = QHBoxLayout()
            self.external.setText(collection.bib_export_path or "")
            self.external.setPlaceholderText("No external .bib file")
            row.addWidget(self.external, 1)
            browse = QPushButton("Browse...")
            browse.clicked.connect(self._browse)
            row.addWidget(browse)
            clear = QPushButton("Clear")
            clear.clicked.connect(self.external.clear)
            row.addWidget(clear)
            v.addLayout(row)
            layout.addWidget(bib)

        buttons = _buttons("OK")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _browse(self) -> None:
        start = self.external.text() or str(Path.home())
        path, _ = QFileDialog.getSaveFileName(
            self, "External .bib file", start, "BibTeX (*.bib)", options=QFileDialog.Option.DontConfirmOverwrite
        )
        if path:
            self.external.setText(path if path.lower().endswith(".bib") else path + ".bib")

    def visible_columns(self) -> set[int]:
        return {col for col, box in self.checks.items() if box.isChecked()} | {int(Col.TITLE)}

    def external_path(self) -> str | None:
        text = self.external.text().strip()
        return text or None


@dataclass(frozen=True)
class PreferenceValues:
    library_root: Path
    contact_email: str
    import_mode: str
    theme_mode: str
    extra_protected_words: tuple[str, ...]
    inbox_enabled: bool = True
    inbox_folder: Path | None = None


THEME_LABELS = (("system", "Use system setting"), ("light", "Light"), ("dark", "Dark"))


class PreferencesDialog(QDialog):
    """Preferences, also used as the first-run welcome (library folder and contact e-mail)."""

    def __init__(self, parent: QWidget | None, *, config: AppConfig, theme_mode: str, first_run: bool = False) -> None:
        super().__init__(parent)
        self.first_run = first_run
        self.setWindowTitle(f"Welcome to {APP_NAME}" if first_run else "Preferences")
        self.setWindowIcon(icons.app_icon())
        self.setMinimumWidth(600)
        layout = QVBoxLayout(self)
        layout.setSpacing(10)
        if first_run:
            intro = QLabel(
                f"<h2 style='margin:0'>Welcome to {APP_NAME}</h2>"
                f"<p style='margin-top:2px'><i>{APP_SUBTITLE}</i></p>"
                f"<p>Choose where your library lives. {APP_NAME} keeps every PDF once in "
                "<code>pdfs/&lt;year&gt;/</code> and writes one <code>.bib</code> file per project in "
                "<code>bibtex/</code> inside this location.</p>"
            )
            intro.setWordWrap(True)
            intro.setTextFormat(Qt.TextFormat.RichText)
            header = QHBoxLayout()
            header.setSpacing(16)
            logo = QLabel()
            logo.setPixmap(icons.brand_pixmap(88, with_name=False))
            logo.setAlignment(Qt.AlignmentFlag.AlignTop)
            header.addWidget(logo)
            header.addWidget(intro, 1)
            layout.addLayout(header)

        library = QGroupBox("Library")
        lv = QVBoxLayout(library)
        row = QHBoxLayout()
        self.root = QLineEdit(str(config.library_root))
        row.addWidget(self.root, 1)
        browse = QPushButton("Browse...")
        browse.setAutoDefault(False)
        browse.clicked.connect(self._browse)
        row.addWidget(browse)
        lv.addWidget(QLabel("Library folder"))
        lv.addLayout(row)
        if not first_run:
            hint = QLabel(f"Changing it reopens {APP_NAME} on that folder; existing files are not moved.")
            hint.setObjectName("fieldLabel")
            lv.addSpacing(2)
            lv.addWidget(hint)
        lv.addSpacing(4)
        lv.addWidget(QLabel("When adding PDFs"))
        self.move_radio = QRadioButton("Move them into the library (keeps a single copy)")
        self.copy_radio = QRadioButton("Copy them and leave the originals where they are")
        group = QButtonGroup(self)
        group.addButton(self.move_radio)
        group.addButton(self.copy_radio)
        (self.copy_radio if config.import_mode == "copy" else self.move_radio).setChecked(True)
        lv.addWidget(self.move_radio)
        lv.addWidget(self.copy_radio)
        layout.addWidget(library)

        inbox = QGroupBox("Inbox folder")
        iv = QVBoxLayout(inbox)
        self.inbox_enabled = QCheckBox("Import PDFs saved into the inbox folder automatically")
        self.inbox_enabled.setChecked(config.inbox_enabled)
        iv.addWidget(self.inbox_enabled)
        inbox_row = QHBoxLayout()
        self.inbox_folder = QLineEdit(str(config.inbox_folder) if config.inbox_folder else "")
        self.inbox_folder.setPlaceholderText(str(Path(config.library_root) / "inbox") + "  (default)")
        inbox_row.addWidget(self.inbox_folder, 1)
        inbox_browse = QPushButton("Browse...")
        inbox_browse.setAutoDefault(False)
        inbox_browse.clicked.connect(self._browse_inbox)
        inbox_row.addWidget(inbox_browse)
        iv.addLayout(inbox_row)
        inbox_hint = QLabel(
            "Point your browser's download folder here: new PDFs are imported and moved into the library; "
            "duplicates go to the Duplicates sub-folder."
        )
        inbox_hint.setObjectName("fieldLabel")
        inbox_hint.setWordWrap(True)
        iv.addWidget(inbox_hint)
        layout.addWidget(inbox)
        inbox.setVisible(not first_run)  # first run: defaults (inbox inside the library, enabled)

        online = QGroupBox("Online metadata (Crossref, arXiv, OpenAlex)")
        ov = QVBoxLayout(online)
        ov.addWidget(QLabel("Contact e-mail (optional)"))
        email = "" if config.contact_email == DEFAULT_CONTACT_EMAIL else config.contact_email
        self.email = QLineEdit(email)
        self.email.setPlaceholderText("name@university.edu")
        ov.addWidget(self.email)
        note = QLabel(
            "Sent only to Crossref and OpenAlex in the User-Agent header, as their etiquette asks; it gives "
            "faster, more reliable responses."
        )
        note.setObjectName("fieldLabel")
        note.setWordWrap(True)
        ov.addWidget(note)
        layout.addWidget(online)

        self.theme = QComboBox()
        for key, label in THEME_LABELS:
            self.theme.addItem(label, key)
        self.theme.setCurrentIndex(max(0, self.theme.findData(theme_mode)))
        self.words = QPlainTextEdit("\n".join(config.extra_protected_words))
        self.words.setPlaceholderText("One word per line, e.g. Duffing")
        self.words.setFixedHeight(90)
        if not first_run:
            look = QGroupBox("Appearance")
            av = QHBoxLayout(look)
            av.addWidget(QLabel("Theme"))
            av.addWidget(self.theme, 1)
            layout.addWidget(look)
            bib = QGroupBox("BibTeX")
            bv = QVBoxLayout(bib)
            bv.addWidget(QLabel("Additional words to keep capitalised in titles ({...} protection)"))
            bv.addWidget(self.words)
            layout.addWidget(bib)

        buttons = _buttons("Get started" if first_run else "Save")
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _browse(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Library folder", self.root.text() or str(Path.home()))
        if folder:
            self.root.setText(folder)

    def _browse_inbox(self) -> None:
        start = self.inbox_folder.text() or self.root.text() or str(Path.home())
        folder = QFileDialog.getExistingDirectory(self, "Inbox folder", start)
        if folder:
            self.inbox_folder.setText(folder)

    def _accept(self) -> None:
        root = Path(self.root.text().strip()).expanduser()
        if not self.root.text().strip() or not root.is_absolute():
            QMessageBox.warning(self, self.windowTitle(), "Please choose an absolute folder path for the library.")
            return
        self.accept()

    def values(self) -> PreferenceValues:
        words = tuple(w.strip() for w in self.words.toPlainText().splitlines() if w.strip())
        return PreferenceValues(
            library_root=Path(self.root.text().strip()).expanduser(),
            contact_email=self.email.text().strip() or DEFAULT_CONTACT_EMAIL,
            import_mode="copy" if self.copy_radio.isChecked() else "move",
            theme_mode=str(self.theme.currentData()),
            extra_protected_words=words,
            inbox_enabled=self.inbox_enabled.isChecked(),
            inbox_folder=Path(inbox).expanduser() if (inbox := self.inbox_folder.text().strip()) else None,
        )


# --------------------------------------------------------------------------- Mendeley import
class MendeleyImportDialog(QDialog):
    """Options for importing a Mendeley Desktop library from its BibTeX export (one or several .bib files)."""

    COL_FOLDER, COL_FILE, COL_ENTRIES, COL_GROUPS, COL_FILES = range(5)

    def __init__(self, parent: QWidget | None, *, start_dir: Path | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Import Mendeley Library")
        self.setWindowIcon(icons.app_icon())
        self.setMinimumWidth(720)
        self._start = start_dir or Path.home()
        self._summaries: dict[Path, BibFileSummary] = {}
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 14)
        layout.setSpacing(10)
        intro = QLabel(
            "Builds the library from Mendeley's BibTeX export. Projects come from <code>mendeley-groups</code> "
            "or, with Mendeley's one-file-per-folder export, from the files ticked below. Running the import "
            "again only adds what is new (projects and PDFs are added to entries already imported)."
        )
        intro.setWordWrap(True)
        intro.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(intro)

        source = QGroupBox("Source")
        sv = QVBoxLayout(source)
        sv.setSpacing(4)
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["Project", "File", "Entries", "In Mendeley folders", "With PDF path"])
        self.table.verticalHeader().hide()
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(
            QAbstractItemView.EditTrigger.DoubleClicked | QAbstractItemView.EditTrigger.EditKeyPressed
        )
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(self.COL_FOLDER, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(self.COL_FILE, QHeaderView.ResizeMode.Stretch)
        for col in (self.COL_ENTRIES, self.COL_GROUPS, self.COL_FILES):
            header.setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents)
        self.table.setFixedHeight(150)
        self.table.itemChanged.connect(lambda _item: self._update_hint())
        sv.addWidget(self.table)
        sv.addSpacing(6)
        buttons_row = QHBoxLayout()
        add = QPushButton("Add .bib files...")
        add.setAutoDefault(False)
        add.clicked.connect(self._browse_bibs)
        remove = QPushButton("Remove")
        remove.setAutoDefault(False)
        remove.clicked.connect(self._remove_selected)
        buttons_row.addWidget(add)
        buttons_row.addWidget(remove)
        self.hint = QLabel()
        self.hint.setObjectName("fieldLabel")
        buttons_row.addWidget(self.hint, 1)
        sv.addLayout(buttons_row)
        sv.addSpacing(6)

        def row(caption: str, edit: QLineEdit, slot: object, hint: str) -> None:
            sv.addWidget(QLabel(caption))
            line = QHBoxLayout()
            line.addWidget(edit, 1)
            line.addWidget(self._browse_button(slot))
            sv.addLayout(line)
            label = QLabel(hint)  # single line: wrapped labels inside group boxes get too little height
            label.setObjectName("fieldLabel")
            sv.addWidget(label)
            sv.addSpacing(4)

        self.pdf_folder = QLineEdit(str(default_mendeley_pdf_folder() or ""))
        self.pdf_folder.setPlaceholderText("Folder Mendeley organised the PDFs in (optional)")
        row(
            "PDF folder",
            self.pdf_folder,
            self._browse_pdf_folder,
            "Searched by file name when a recorded path no longer exists (folder moved or synced).",
        )
        databases = find_mendeley_databases()
        self.database = QLineEdit(str(databases[0]) if databases else "")
        self.database.setPlaceholderText("<email>@www.mendeley.com.sqlite (optional)")
        row(
            "Mendeley database",
            self.database,
            self._browse_database,
            "Optional: favourites, read status, sub-folders (Mendeley 1.19.5+ encrypts it: .bib only).",
        )
        layout.addWidget(source)

        how = QGroupBox("How")
        hv = QGridLayout(how)
        hv.setVerticalSpacing(8)
        self.file_mode = QComboBox()
        for label, mode in (
            ("Leave them where they are (linked): the Mendeley folder is not touched", "link"),
            ("Copy them into the library as pdfs/<year>/<key>.pdf (originals kept)", "copy"),
            ("Move them into the library as pdfs/<year>/<key>.pdf", "move"),
        ):
            self.file_mode.addItem(label, mode)
        self.key_mode = QComboBox()
        self.key_mode.addItem("Keep Mendeley's keys: \\cite{} in existing manuscripts keeps working", True)
        self.key_mode.addItem(f"{APP_NAME} naming: Surname_Year, Surname1_Surname2_Year, Surname_et_al_Year", False)
        hv.addWidget(QLabel("PDF files"), 0, 0)
        hv.addWidget(self.file_mode, 0, 1)
        hv.addWidget(QLabel("Citation keys"), 1, 0)
        hv.addWidget(self.key_mode, 1, 1)
        hv.setColumnStretch(1, 1)
        layout.addWidget(how)

        more = QGroupBox("Options")
        mv = QGridLayout(more)
        self.groups = QCheckBox("Create projects from mendeley-groups")
        self.groups.setChecked(True)
        self.notes = QCheckBox("Import notes (annote) and tags")
        self.notes.setChecked(True)
        self.fulltext = QCheckBox("Index the full text of the PDFs (slower)")
        self.fulltext.setChecked(True)
        self.take_over = QCheckBox("Keep writing each ticked project to its .bib file")
        self.take_over.setToolTip(
            "Manuscripts that use these .bib files keep compiling: the project's BibTeX is written back to the "
            "same file from now on (Mendeley should no longer sync it)."
        )
        mv.addWidget(self.groups, 0, 0)
        mv.addWidget(self.notes, 0, 1)
        mv.addWidget(self.fulltext, 1, 0)
        mv.addWidget(self.take_over, 1, 1)
        layout.addWidget(more)

        buttons = _buttons("Import")
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._update_hint()

    # ------------------------------------------------------------------ files
    def _browse_button(self, slot: object) -> QPushButton:
        button = QPushButton("Browse...")
        button.setAutoDefault(False)
        button.clicked.connect(slot)
        return button

    def _browse_bibs(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(self, "Mendeley BibTeX export", str(self._start), "BibTeX (*.bib)")
        if paths:
            self._start = Path(paths[0]).parent
            self.add_bib_files([Path(p) for p in paths])

    def add_bib_files(self, paths: list[Path]) -> None:
        """Add files to the table with their summary; several files default to one project per file."""
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            for path in paths:
                if path not in self._summaries:
                    self._summaries[path] = summarize_bib_file(path)
        finally:
            QApplication.restoreOverrideCursor()
        existing = self.bib_paths()
        all_paths = [*existing, *(p for p in paths if p not in existing)]
        self.table.blockSignals(True)
        self.table.setRowCount(0)
        for path in all_paths:
            summary = self._summaries[path]
            r = self.table.rowCount()
            self.table.insertRow(r)
            folder = QTableWidgetItem(path.stem)
            folder.setFlags(folder.flags() | Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEditable)
            folder.setCheckState(
                Qt.CheckState.Checked if suggest_bib_folder(path, len(all_paths)) else Qt.CheckState.Unchecked
            )
            folder.setToolTip("Tick to put every entry of this file into a project with this name (editable)")
            folder.setData(Qt.ItemDataRole.UserRole, str(path))
            self.table.setItem(r, self.COL_FOLDER, folder)
            name = QTableWidgetItem(path.name)
            name.setFlags(name.flags() & ~Qt.ItemFlag.ItemIsEditable)
            name.setToolTip(summary.error or str(path))
            self.table.setItem(r, self.COL_FILE, name)
            for col, value in (
                (self.COL_ENTRIES, summary.entries),
                (self.COL_GROUPS, summary.with_groups),
                (self.COL_FILES, summary.with_files),
            ):
                item = QTableWidgetItem(str(value) if not summary.error else "-")
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                self.table.setItem(r, col, item)
        self.table.blockSignals(False)
        self._update_hint()

    def _remove_selected(self) -> None:
        rows = sorted({i.row() for i in self.table.selectedIndexes()}, reverse=True)
        for r in rows:
            self.table.removeRow(r)
        self._update_hint()

    def _folder_items(self) -> list[QTableWidgetItem]:
        items = (self.table.item(r, self.COL_FOLDER) for r in range(self.table.rowCount()))
        return [item for item in items if item is not None]

    def bib_paths(self) -> list[Path]:
        return [Path(str(item.data(Qt.ItemDataRole.UserRole))) for item in self._folder_items()]

    def bib_folders(self) -> dict[Path, str]:
        folders: dict[Path, str] = {}
        for item in self._folder_items():
            path = Path(str(item.data(Qt.ItemDataRole.UserRole)))
            checked = item.checkState() == Qt.CheckState.Checked
            folders[path] = " ".join(item.text().split()) if checked else ""
        return folders

    def _update_hint(self) -> None:
        summaries = [self._summaries[p] for p in self.bib_paths() if p in self._summaries]
        entries = sum(s.entries for s in summaries)
        grouped = sum(s.with_groups for s in summaries)
        ticked = sum(1 for name in self.bib_folders().values() if name)
        if not summaries:
            text = "Add one or more .bib files exported by Mendeley."
        elif grouped == 0 and ticked == 0:
            text = f"{entries} entries, none in a Mendeley folder: tick files to create projects."
        else:
            groups = len({g for s in summaries for g in s.groups})
            parts = [f"{entries} entries"]
            if grouped:
                parts.append(f"{groups} Mendeley folder(s)")
            if ticked:
                parts.append(f"{ticked} project(s) from files")
            text = "; ".join(parts) + "."
        self.hint.setText(text)
        self.take_over.setEnabled(ticked > 0)

    def _browse_pdf_folder(self) -> None:
        start = self.pdf_folder.text() or str(self._start)
        folder = QFileDialog.getExistingDirectory(self, "Folder with the Mendeley PDFs", start)
        if folder:
            self.pdf_folder.setText(folder)

    def _browse_database(self) -> None:
        start = self.database.text() or str(self._start)
        path, _ = QFileDialog.getOpenFileName(self, "Mendeley database", start, "SQLite (*.sqlite)")
        if path:
            self.database.setText(path)

    def _accept(self) -> None:
        paths = self.bib_paths()
        if not paths or any(not p.is_file() for p in paths):
            QMessageBox.warning(self, self.windowTitle(), "Add one or more existing .bib files.")
            return
        if self.pdf_folder.text().strip() and not Path(self.pdf_folder.text().strip()).is_dir():
            QMessageBox.warning(self, self.windowTitle(), "The PDF folder does not exist.")
            return
        self.accept()

    def options(self) -> MendeleyImportOptions:
        pdf_folder = self.pdf_folder.text().strip()
        database = self.database.text().strip()
        folders = self.bib_folders()
        return MendeleyImportOptions(
            bib_paths=self.bib_paths(),
            pdf_folder=Path(pdf_folder) if pdf_folder else None,
            file_mode=str(self.file_mode.currentData()),
            keep_keys=bool(self.key_mode.currentData()),
            import_groups=self.groups.isChecked(),
            bib_folders=folders,
            take_over_bib_files=self.take_over.isEnabled() and self.take_over.isChecked(),
            import_notes=self.notes.isChecked(),
            index_fulltext=self.fulltext.isChecked(),
            database=Path(database) if database and Path(database).is_file() else None,
        )


class FindPdfsDialog(QDialog):
    """Tools > Find Missing PDFs: where to look and what to do with the files found."""

    def __init__(self, parent: QWidget | None, *, folder: Path | None, missing: int) -> None:
        super().__init__(parent)
        self.setWindowTitle("Find Missing PDFs")
        self.setWindowIcon(icons.app_icon())
        self.setMinimumWidth(620)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 14)
        layout.setSpacing(10)
        intro = QLabel(
            f"{missing} reference(s) have no PDF. The folder below (and its sub-folders) is searched for the "
            "file name Mendeley recorded, a file named after the citation key, or a Mendeley-style name "
            "<i>Authors - Year - Title.pdf</i> matching the title and year. Ambiguous matches are listed, "
            "never guessed."
        )
        intro.setWordWrap(True)
        intro.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(intro)
        layout.addWidget(QLabel("Folder"))
        line = QHBoxLayout()
        self.folder = QLineEdit(str(folder or ""))
        line.addWidget(self.folder, 1)
        browse = QPushButton("Browse...")
        browse.setAutoDefault(False)
        browse.clicked.connect(self._browse)
        line.addWidget(browse)
        layout.addLayout(line)
        layout.addWidget(QLabel("PDFs found"))
        self.mode = QComboBox()
        self.mode.addItem("Leave them where they are (linked)", "link")
        self.mode.addItem("Copy them into the library as pdfs/<year>/<key>.pdf", "copy")
        self.mode.addItem("Move them into the library as pdfs/<year>/<key>.pdf", "move")
        layout.addWidget(self.mode)
        buttons = _buttons("Search")
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _browse(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Folder with PDFs", self.folder.text() or str(Path.home()))
        if folder:
            self.folder.setText(folder)

    def _accept(self) -> None:
        if not Path(self.folder.text().strip()).is_dir():
            QMessageBox.warning(self, self.windowTitle(), "Choose an existing folder.")
            return
        self.accept()

    def values(self) -> tuple[Path, str]:
        return Path(self.folder.text().strip()), str(self.mode.currentData())


class ZipExportDialog(QDialog):
    """Right-click on a project > Export PDFs as ZIP: what goes into the archive (``core/export.py``).

    ``planner`` is ``Library.plan_project_zip`` bound to the project; it is called again whenever an option changes so
    the counts and the size are always those of the archive that will be written.
    """

    def __init__(
        self, parent: QWidget | None, *, planner: Callable[[ZipOptions], ZipPlan], options: ZipOptions
    ) -> None:
        super().__init__(parent)
        self._planner = planner
        self._plan = planner(options)
        self.setWindowTitle(f"Export PDFs — {self._plan.name}")
        self.setWindowIcon(icons.app_icon())
        self.setMinimumWidth(560)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 14)
        layout.setSpacing(10)
        intro = QLabel(
            f"One ZIP file with the PDFs of <b>{escape(self._plan.name)}</b>, each named after its citation key, "
            "ready to send to colleagues. Your library is not changed."
        )
        intro.setWordWrap(True)
        intro.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(intro)
        self.bib = QCheckBox(f"Include {self._plan.folder}.bib (the BibTeX entries, with the same keys)")
        self.bib.setChecked(options.include_bib)
        self.supplements = QCheckBox()
        self.supplements.setChecked(options.include_supplements)
        self.subprojects = QCheckBox()
        self.subprojects.setChecked(options.include_subprojects and self._plan.subprojects > 0)
        self.subprojects.setVisible(self._plan.subprojects > 0)
        for box in (self.bib, self.supplements, self.subprojects):
            layout.addWidget(box)
            box.toggled.connect(self._refresh)
        self.summary = QLabel()
        self.summary.setObjectName("zipSummary")
        self.summary.setWordWrap(True)
        self.summary.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(self.summary)
        buttons = _buttons("Export...")
        self.export_button = next(b for b in buttons.buttons() if b.objectName() == "primary")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._refresh(replan=False)

    def options(self) -> ZipOptions:
        return ZipOptions(
            include_bib=self.bib.isChecked(),
            include_supplements=self.supplements.isChecked(),
            include_subprojects=self.subprojects.isChecked(),
        )

    def plan(self) -> ZipPlan:
        return self._plan

    def _refresh(self, *_args: Any, replan: bool = True) -> None:
        if replan:
            self._plan = self._planner(self.options())
        plan = self._plan
        n = plan.subprojects
        self.subprojects.setText(f"Include the references of its {n} sub-project{'s' if n != 1 else ''}")
        found = plan.supplements_found
        self.supplements.setEnabled(found > 0 or self.supplements.isChecked())
        self.supplements.setText(
            f"Include supplementary files ({found} file{'s' if found != 1 else ''}, "
            f"{format_size(plan.supplements_bytes)})"
            if found
            else "Include supplementary files (none on this device)"
        )
        files = plan.pdfs + plan.supplements
        lines = [
            f"<b>{plan.pdfs} PDF{'s' if plan.pdfs != 1 else ''}</b>"
            + (f" + {plan.supplements} supplementary file{'s' if plan.supplements != 1 else ''}" if plan.supplements
               else "")
            + f" from {plan.references} reference{'s' if plan.references != 1 else ''}"
            + f" · about {format_size(plan.total_bytes)}"
        ]  # fmt: skip
        if plan.missing:
            m = len(plan.missing)
            lines.append(
                f"{m} {'item is' if m == 1 else 'items are'} not included (no PDF, or the file is not on this "
                "device); the list is shown after the export."
            )
        if not files:
            lines.append("There is nothing to export: none of these references has a PDF on this device.")
        self.summary.setText("<br>".join(lines))
        self.export_button.setEnabled(files > 0)


def show_zip_report(parent: QWidget | None, plan: ZipPlan, report: ZipReport) -> bool:
    """Summary after a ZIP export; True if the user asked to see the archive in its folder."""
    parts = [f"{report.pdfs} PDF{'s' if report.pdfs != 1 else ''}"]
    if report.supplements:
        parts.append(f"{report.supplements} supplementary file{'s' if report.supplements != 1 else ''}")
    if report.bib:
        parts.append(plan.bib_arcname.rpartition("/")[2])
    text = (
        f"Exported {', '.join(parts[:-1]) + ' and ' + parts[-1] if len(parts) > 1 else parts[0]} to "
        f"{report.target.name} ({format_size(report.size)})."
    )
    details: list[str] = []
    not_included = [*plan.missing, *(f"{s} (could not be read)" for s in report.skipped)]
    if not_included:
        text += f"\n\n{len(not_included)} item(s) could not be included (see details)."
        details = ["Not included:", *not_included]
    box = QMessageBox(parent)
    box.setWindowTitle("Export PDFs as ZIP")
    box.setIcon(QMessageBox.Icon.Warning if report.skipped else QMessageBox.Icon.Information)
    box.setText(text)
    if details:
        box.setDetailedText("\n".join(details))
    reveal = box.addButton("Show in Folder", QMessageBox.ButtonRole.ActionRole)
    box.addButton(QMessageBox.StandardButton.Ok)
    box.exec()
    return box.clickedButton() is reveal


def show_attach_report(parent: QWidget | None, report: AttachReport, *, search: bool) -> None:
    lines = [f"PDFs attached: {len(report.attached)}."]
    if search:
        lines.insert(0, f"Searched {report.scanned} PDF file(s) for {report.searched} document(s) without a PDF.")
        if report.rules:
            names = {"recorded": "recorded file name", "key": "citation key", "title": "title and year",
                     "fuzzy": "title in file name"}  # fmt: skip
            lines.append("Matched by " + ", ".join(f"{names.get(k, k)}: {v}" for k, v in report.rules.items()) + ".")
    details: list[str] = []
    if report.ambiguous:
        lines.append(f"{len(report.ambiguous)} ambiguous match(es) left for you to attach by hand (see details).")
        details += ["Ambiguous:", *report.ambiguous, ""]
    if report.failed:
        lines.append(f"{len(report.failed)} could not be attached (see details).")
        details += ["Not attached:", *report.failed]
    box = QMessageBox(parent)
    box.setWindowTitle("Find Missing PDFs" if search else "Attach PDF")
    box.setIcon(QMessageBox.Icon.Warning if report.failed else QMessageBox.Icon.Information)
    box.setText("\n".join(lines))
    if details:
        box.setDetailedText("\n".join(details))
    box.exec()


def show_import_report(parent: QWidget | None, r: MendeleyImportReport) -> None:
    """Summary of a Mendeley import; the details (missing PDFs, failures, warnings) can be copied."""
    lines = [
        f"{r.imported} of {r.total} references imported" + (" (cancelled)" if r.cancelled else "") + ".",
        f"PDFs linked in place: {r.linked}; copied/moved into the library: {r.stored}; "
        f"references without PDF: {r.without_pdf}.",
    ]
    if r.duplicates:
        lines.append(f"{r.duplicates} were already in the library (folders and flags merged).")
    if r.attached_to_existing:
        lines.append(f"PDFs found for {r.attached_to_existing} of them that had none.")
    if r.supplements:
        lines.append(f"{r.supplements} further files attached as supplementary files.")
    if r.folders:
        lines.append(f"{r.folders} folders used or created.")
    elif r.total:
        lines.append("No folders: the files have no mendeley-groups and no file was ticked as a folder.")
    if r.database_matches:
        lines.append(f"{r.database_matches} references matched in the Mendeley database.")
    details: list[str] = []
    if r.missing_pdfs:
        lines.append(f"{len(r.missing_pdfs)} recorded PDFs were not found (imported without PDF; see details).")
        details += ["PDFs not found:", *r.missing_pdfs, ""]
    if r.failed:
        lines.append(f"{len(r.failed)} references failed (see details).")
        details += ["Failures:", *r.failed, ""]
    if r.warnings:
        lines.append(f"{len(r.warnings)} warning(s) (see details).")
        details += ["Warnings:", *r.warnings]
    box = QMessageBox(parent)
    box.setWindowTitle("Mendeley Import")
    box.setIcon(QMessageBox.Icon.Warning if r.failed else QMessageBox.Icon.Information)
    box.setText("\n".join(lines))
    if details:
        box.setDetailedText("\n".join(details))
    box.exec()


def confirm_naming_rule(parent: QWidget | None, outdated: list[tuple[Paper, str]]) -> bool:
    """Ask before re-keying: changed keys break \\cite{} commands in existing manuscripts."""
    sample = "\n".join(f"{p.citation_key}  \u2192  {base}" for p, base in outdated[:12])
    more = f"\n... and {len(outdated) - 12} more" if len(outdated) > 12 else ""
    box = QMessageBox(parent)
    box.setWindowTitle("Apply Naming Rule")
    box.setIcon(QMessageBox.Icon.Question)
    box.setText(
        f"Rename {len(outdated)} citation key(s) to the current naming rule?\n\n{sample}{more}\n\n"
        "Managed PDFs are renamed with their key; linked PDFs stay untouched. Manuscripts that cite the old "
        "keys will need updating (collision suffixes a, b, ... are added where needed)."
    )
    box.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel)
    box.setDefaultButton(QMessageBox.StandardButton.Cancel)
    return box.exec() == QMessageBox.StandardButton.Yes


SHORTCUTS = (
    ("Ctrl+K", "Search, or type > to run a command"),
    ("Ctrl+O", "Import PDFs"),
    ("Ctrl+Shift+C", r"Copy \cite{...} for the selected references"),
    ("Ctrl+Shift+B", "Copy BibTeX entries"),
    ("Enter / double-click", "Open PDF"),
    ("Ctrl+J", "Study and ask AI (Esc returns)"),
    ("Ctrl+Return", "Mark as verified (Inbox)"),
    ("Ctrl+1 / Ctrl+2", "Rows view / table view"),
    ("Delete", "Delete references from the library"),
    ("F2", "Rename project"),
    ("F5", "Rewrite .bib files"),
    ("F6", "Synchronise with the other devices"),
    ("Ctrl+,", "Preferences"),
)


@dataclass(frozen=True)
class DeviceSyncValues:
    folder: Path
    device_name: str


class DeviceSyncDialog(QDialog):
    """Tools > Share Library with Other Devices: choose the synchronised folder, see the devices."""

    def __init__(self, parent: QWidget | None, library: Any) -> None:
        super().__init__(parent)
        self.library = library
        engine = library.sync
        self.setWindowTitle("Share Library with Other Devices")
        self.setWindowIcon(icons.app_icon())
        self.setMinimumWidth(640)
        layout = QVBoxLayout(self)
        layout.setSpacing(10)
        intro = QLabel(
            f"<p>{APP_NAME} can keep this library identical on several devices (another PC, the Android app) "
            "through a folder that Dropbox, Syncthing, Qsync or a similar tool keeps synchronised.</p>"
            "<p>The folder holds the PDFs, the <code>.bib</code> files and one small change journal per device "
            "(<code>sync/</code>). The database itself stays on each device, so two devices never write the "
            "same file and edits made offline on both sides are merged.</p>"
        )
        intro.setWordWrap(True)
        intro.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(intro)

        form = QFormLayout()
        self.folder = QLineEdit(str(library.root))
        browse = QPushButton("Browse...")
        browse.clicked.connect(self._browse)
        row = QHBoxLayout()
        row.addWidget(self.folder, 1)
        row.addWidget(browse)
        form.addRow("Synchronised folder:", row)
        self.device = QLineEdit(engine.device_name if engine is not None else library.config.device_name)
        self.device.setPlaceholderText("Name shown to the other devices, e.g. Office PC")
        form.addRow("This device:", self.device)
        layout.addLayout(form)

        self.hint = QLabel()
        self.hint.setWordWrap(True)
        self.hint.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(self.hint)
        self.suggestions = QVBoxLayout()
        layout.addLayout(self.suggestions)
        outside = library.linked_folders_outside(limit=3)
        if outside:
            total = sum(count for _folder, count in library.linked_folders_outside(limit=1000))
            self.hint.setText(
                f"<b>{total} linked PDFs are outside the library folder</b> and would not reach the other devices. "
                "Nothing needs to be copied: make the folder where they already are (if it is synchronised) the "
                f"library folder. {APP_NAME} then only adds its own <code>pdfs/</code>, <code>bibtex/</code> and "
                "<code>sync/</code> sub-folders there."
            )
            for folder, count in outside:
                button = QPushButton(f"Use  {folder}   ({count} PDFs)")
                button.setStyleSheet("text-align: left; padding: 5px 10px;")
                button.clicked.connect(lambda _checked=False, f=folder: self.folder.setText(str(Path(f))))
                self.suggestions.addWidget(button)
        elif engine is None:
            self.hint.setText("All PDFs of this library are inside the library folder.")

        if engine is not None:
            devices = engine.devices()
            table = QTableWidget(len(devices), 3)
            table.setHorizontalHeaderLabels(["Device", "Application", "Last change"])
            table.verticalHeader().hide()
            table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
            table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
            for index, info in enumerate(devices):
                when = datetime.fromtimestamp(info.last_change).strftime("%Y-%m-%d %H:%M") if info.last_change else ""
                name = info.name + ("  (this device)" if info.is_self else "")
                for column, text in enumerate((name, info.app, when)):
                    table.setItem(index, column, QTableWidgetItem(text))
            table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
            table.setMaximumHeight(56 + 30 * max(1, len(devices)))
            layout.addWidget(table)
            conflicts = engine.conflict_copies()
            if conflicts:
                warn = QLabel(
                    f"<b>The synchronisation tool left {len(conflicts)} conflicting copies in sync/</b> "
                    f"(for example {conflicts[0].name}). They are ignored; you can delete them."
                )
                warn.setWordWrap(True)
                layout.addWidget(warn)
        self.buttons = _buttons("Synchronise Now" if engine is not None else "Share Library")
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)

    def _browse(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Synchronised folder", self.folder.text())
        if path:
            self.folder.setText(path)

    def values(self) -> DeviceSyncValues:
        return DeviceSyncValues(Path(self.folder.text().strip()).expanduser(), self.device.text().strip())


def show_about(parent: QWidget | None, library_root: Path) -> None:
    rows = "".join(f"<tr><td><b>{k}</b></td><td>&nbsp;&nbsp;{v}</td></tr>" for k, v in SHORTCUTS)
    box = QMessageBox(parent)
    box.setWindowTitle(f"About {APP_NAME}")
    box.setIconPixmap(icons.brand_pixmap(112))
    box.setTextFormat(Qt.TextFormat.RichText)
    box.setText(
        f"<h3>{APP_NAME} {__version__}</h3>"
        f"<p><b>{APP_SUBTITLE}</b><br>Local, file-first reference manager for LaTeX writing.</p>"
        f"<p>Author: {APP_AUTHOR} &middot; <a href='mailto:{APP_AUTHOR_EMAIL}'>{APP_AUTHOR_EMAIL}</a></p>"
        f"<p>Library: <code>{library_root}</code></p>"
        f"<table>{rows}</table>"
    )
    box.exec()
