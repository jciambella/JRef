"""Right column: the reference sheet -- one scrolling page per reference with click-to-edit fields, a Cite block
(the \\cite command and the exact BibTeX entry written to the .bib files), project chips, files, notes and the PDF
outline, and the actions Study and ask AI / Look up metadata."""

from __future__ import annotations

import re
from collections.abc import Sequence
from html import escape
from typing import Any

from PySide6.QtCore import QEvent, QObject, QPoint, QRect, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QFocusEvent, QGuiApplication, QKeyEvent, QResizeEvent
from PySide6.QtWidgets import (
    QButtonGroup,
    QComboBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLayout,
    QLayoutItem,
    QLineEdit,
    QMenu,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QTextEdit,
    QToolButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..core.extractor import PdfOutline
from ..core.models import Attachment, Paper
from . import icons, theme

ENTRY_TYPES: tuple[tuple[str, str], ...] = (
    ("article", "JOURNAL ARTICLE"),
    ("book", "BOOK"),
    ("incollection", "BOOK CHAPTER"),
    ("inproceedings", "CONFERENCE PAPER"),
    ("phdthesis", "THESIS"),
    ("techreport", "REPORT"),
    ("misc", "OTHER"),
)
CITE_COMMANDS = ("cite", "citep", "citet")
Project = tuple[int, str]  # collection id, name


# --------------------------------------------------------------------------- inline editors
class InlineLineEdit(QLineEdit):
    """Looks like a label until hovered/focused; emits ``committed(field, text)`` on Enter or focus-out."""

    committed = Signal(str, str)

    def __init__(self, field: str, parent: QWidget | None = None, placeholder: str = "") -> None:
        super().__init__(parent)
        self.setObjectName(field)
        self.setProperty("inline", True)
        self.setFrame(False)
        self.field = field
        self._original = ""
        self.setPlaceholderText(placeholder)
        self.editingFinished.connect(self._commit)

    def set_value(self, value: str) -> None:
        self._original = value
        if self.text() != value:
            self.setText(value)
            self.setCursorPosition(0)

    def value(self) -> str:
        return self.text().strip()

    def _commit(self) -> None:
        text = self.value()
        if text != self._original:
            self._original = text
            self.committed.emit(self.field, text)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self.setText(self._original)
            self.clearFocus()
            return
        super().keyPressEvent(event)


class InlineTextEdit(QTextEdit):
    """Auto-growing, word-wrapping inline editor (title, authors, abstract, tags, keywords)."""

    committed = Signal(str, str)

    def __init__(
        self,
        field: str,
        parent: QWidget | None = None,
        *,
        multiline: bool = False,
        placeholder: str = "",
    ) -> None:
        super().__init__(parent)
        self.setObjectName(field)
        self.setProperty("inline", True)
        self.field = field
        self.multiline = multiline
        self._original = ""
        self.setAcceptRichText(False)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setLineWrapMode(QTextEdit.LineWrapMode.WidgetWidth)
        self.setTabChangesFocus(True)
        self.setPlaceholderText(placeholder)
        self.document().setDocumentMargin(2)
        self.document().contentsChanged.connect(self._fit)

    def set_value(self, value: str) -> None:
        self._original = value
        if self.toPlainText() != value:
            self.setPlainText(value)
        self._fit()

    def value(self) -> str:
        text = self.toPlainText()
        return text.strip() if not self.multiline else text.rstrip()

    def _fit(self) -> None:
        doc = self.document()
        doc.setTextWidth(max(10, self.viewport().width()))
        height = int(doc.size().height()) + 2 * self.frameWidth() + 4
        self.setFixedHeight(max(height, self.fontMetrics().lineSpacing() + 10))

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._fit()

    def focusOutEvent(self, event: QFocusEvent) -> None:
        super().focusOutEvent(event)
        self._commit()

    def keyPressEvent(self, event: QKeyEvent) -> None:
        key = event.key()
        if key == Qt.Key.Key_Escape:
            self.set_value(self._original)
            self.clearFocus()
            return
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            ctrl = bool(event.modifiers() & Qt.KeyboardModifier.ControlModifier)
            if not self.multiline or ctrl:
                self._commit()
                return
        super().keyPressEvent(event)

    def _commit(self) -> None:
        text = self.value()
        if text != self._original:
            self._original = text
            self.committed.emit(self.field, text)


class FlowLayout(QLayout):
    """Left-to-right layout that wraps its items (project chips)."""

    def __init__(self, parent: QWidget | None = None, spacing: int = 6) -> None:
        super().__init__(parent)
        self._items: list[QLayoutItem] = []
        self.setContentsMargins(0, 0, 0, 0)
        self.setSpacing(spacing)

    def addItem(self, item: QLayoutItem) -> None:
        self._items.append(item)

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, index: int) -> QLayoutItem | None:
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index: int) -> QLayoutItem | None:
        return self._items.pop(index) if 0 <= index < len(self._items) else None

    def expandingDirections(self) -> Qt.Orientation:
        return Qt.Orientation(0)

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, width: int) -> int:
        return self._arrange(QRect(0, 0, width, 0), apply=False)

    def setGeometry(self, rect: QRect) -> None:
        super().setGeometry(rect)
        self._arrange(rect, apply=True)

    def sizeHint(self) -> QSize:
        return self.minimumSize()

    def minimumSize(self) -> QSize:
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        return size

    def _arrange(self, rect: QRect, *, apply: bool) -> int:
        x, y, line = rect.x(), rect.y(), 0
        for item in self._items:
            hint = item.sizeHint()
            if x + hint.width() > rect.right() and line > 0:
                x, y, line = rect.x(), y + line + self.spacing(), 0
            if apply:
                item.setGeometry(QRect(QPoint(x, y), hint))
            x += hint.width() + self.spacing()
            line = max(line, hint.height())
        return y + line - rect.y()


def _label(text: str, name: str = "fieldLabel") -> QLabel:
    label = QLabel(text)
    label.setObjectName(name)
    return label


# --------------------------------------------------------------------------- the sheet body
def highlight_bibtex(entry: str) -> str:
    """HTML of a BibTeX entry with the brace-protected words of the title highlighted (``{Mullins}``)."""
    t = theme.current()
    out = []
    for line in entry.splitlines():
        html = escape(line)
        stripped = line.strip().lower()
        if stripped.startswith(("title", "booktitle")) and "{" in line:
            head, _, value = html.partition("{")
            inner = re.sub(
                r"\{([^{}]+)\}",
                lambda m: f"<span style='background:{t.key_bg}; color:{t.key_text}'>{{{m.group(1)}}}</span>",
                value[: value.rfind("}")],
            )
            html = f"{head}{{{inner}{value[value.rfind('}'):]}"
        out.append(html)
    return "<pre style='margin:0; white-space:pre-wrap'>" + "\n".join(out) + "</pre>"


class DetailsTab(QStackedWidget):
    """The scrolling body of the reference sheet (page 0: a message; page 1: the reference)."""

    fieldEdited = Signal(str, str)
    lookupRequested = Signal(str)  # "auto" | "doi" | "arxiv"
    openFileRequested = Signal()
    attachFileRequested = Signal()
    openUrlRequested = Signal(str)
    revealFileRequested = Signal()
    markReviewedRequested = Signal()
    attachmentAction = Signal(str, int)  # "open" | "reveal" | "rename" | "promote" | "remove", attachment id
    addSupplementsRequested = Signal()
    projectToggled = Signal(int, bool)  # collection id, add (True) / remove (False)
    newProjectRequested = Signal()
    favoriteToggled = Signal()
    citeCopied = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._has_pdf = True
        self._attachment_rows: list[QWidget] = []
        self._paper: Paper | None = None
        self._bibtex = ""
        self._doi = ""
        self.setObjectName("details")
        self.empty = QLabel("No reference selected")
        self.empty.setObjectName("multiSelection")
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty.setWordWrap(True)
        self.addWidget(self.empty)

        body = QWidget()
        body.setObjectName("detailsBody")
        self.scroll = QScrollArea()
        self.scroll.setObjectName("detailsScroll")
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        body.setMinimumWidth(0)
        self.scroll.setWidget(body)
        self.addWidget(self.scroll)

        v = QVBoxLayout(body)
        v.setContentsMargins(18, 14, 18, 16)
        v.setSpacing(6)

        # entry type and star
        top = QHBoxLayout()
        top.setSpacing(4)
        self.type_combo = QComboBox()
        self.type_combo.setObjectName("typeCombo")
        self.type_combo.setToolTip("Entry type (BibTeX)")
        self.type_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
        for key, label in ENTRY_TYPES:
            self.type_combo.addItem(label, key)
        self.type_combo.activated.connect(self._on_type_activated)
        top.addWidget(self.type_combo)
        top.addStretch(1)
        self.star_button = QToolButton()
        self.star_button.setObjectName("iconButton")
        self.star_button.setToolTip("Star")
        self.star_button.setIconSize(QSize(18, 18))
        self.star_button.clicked.connect(self.favoriteToggled)
        top.addWidget(self.star_button)
        v.addLayout(top)

        # not verified yet (replaces the review banner)
        self.verify = QFrame()
        self.verify.setObjectName("verifyCard")
        vv = QVBoxLayout(self.verify)
        vv.setContentsMargins(12, 10, 12, 10)
        vv.setSpacing(4)
        verify_title = QLabel("Not verified yet")
        verify_title.setObjectName("verifyTitle")
        vv.addWidget(verify_title)
        self.verify_text = QLabel()
        self.verify_text.setWordWrap(True)
        vv.addWidget(self.verify_text)
        buttons = QHBoxLayout()
        self.accept_button = QPushButton("Mark as verified")
        self.accept_button.setObjectName("primary")
        self.accept_button.setToolTip("The details are right: take the reference out of the Inbox (Ctrl+Return)")
        self.accept_button.clicked.connect(self.markReviewedRequested)
        again = QPushButton("Look up again")
        again.clicked.connect(lambda: self.lookupRequested.emit("auto"))
        buttons.addWidget(self.accept_button)
        buttons.addWidget(again)
        buttons.addStretch(1)
        vv.addLayout(buttons)
        v.addWidget(self.verify)
        self.banner = self.verify  # name used by code written against 0.7

        self.title = InlineTextEdit("title", placeholder="Title")
        v.addWidget(self.title)
        self.authors = InlineTextEdit("authors", placeholder="Authors (Surname, Given; Surname, Given)")
        v.addWidget(self.authors)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        form.setHorizontalSpacing(12)
        form.setVerticalSpacing(1)
        self.journal = InlineLineEdit("journal", placeholder="Journal, proceedings, school...")
        self.year = InlineLineEdit("year", placeholder="Unknown")
        self.volume = InlineLineEdit("volume")
        self.issue = InlineLineEdit("issue")
        self.pages = InlineLineEdit("pages")
        for caption, widget in (
            ("Venue", self.journal),
            ("Year", self.year),
            ("Volume", self.volume),
            ("Issue", self.issue),
            ("Pages", self.pages),
        ):
            form.addRow(_label(caption), widget)
        v.addLayout(form)
        self.doi_link = QLabel()
        self.doi_link.setObjectName("linkLabel")
        self.doi_link.setCursor(Qt.CursorShape.PointingHandCursor)
        self.doi_link.linkActivated.connect(lambda _link: self.openUrlRequested.emit(f"https://doi.org/{self._doi}"))
        v.addWidget(self.doi_link)

        def section(title: str, widget: QWidget | None = None) -> QLabel:
            label = _label(title.upper(), "sectionLabel")
            v.addWidget(label)
            if widget is not None:
                v.addWidget(widget)
            return label

        # cite block
        self.cite_card = QFrame()
        self.cite_card.setObjectName("citeCard")
        cv = QVBoxLayout(self.cite_card)
        cv.setContentsMargins(12, 10, 12, 12)
        cv.setSpacing(8)
        cite_top = QHBoxLayout()
        cite_top.addWidget(_label("CITE", "sheetKind"))
        cite_top.addStretch(1)
        self.cite_group = QButtonGroup(self)
        self.cite_group.setExclusive(True)
        for i, command in enumerate(CITE_COMMANDS):
            button = QToolButton()
            button.setObjectName("segment")
            button.setProperty("first", i == 0)
            button.setProperty("last", i == len(CITE_COMMANDS) - 1)
            button.setText(f"\\{command}")
            button.setCheckable(True)
            button.setChecked(i == 0)
            self.cite_group.addButton(button, i)
            cite_top.addWidget(button)
        cite_top.setSpacing(0)
        self.cite_group.idClicked.connect(lambda _i: self._update_cite())
        cv.addLayout(cite_top)
        code_row = QHBoxLayout()
        self.cite_code = QLabel()
        self.cite_code.setObjectName("citeCode")
        self.cite_code.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        code_row.addWidget(self.cite_code, 1)
        self.copy_cite = QPushButton("Copy")
        self.copy_cite.setObjectName("strong")
        self.copy_cite.clicked.connect(self.copy_citation)
        code_row.addWidget(self.copy_cite)
        cv.addLayout(code_row)
        self.bib_caption = _label("")
        self.bib_caption.setWordWrap(True)
        cv.addWidget(self.bib_caption)
        self.bibtex_view = QLabel()
        self.bibtex_view.setObjectName("bibtex")
        self.bibtex_view.setTextFormat(Qt.TextFormat.RichText)
        self.bibtex_view.setWordWrap(True)
        self.bibtex_view.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        cv.addWidget(self.bibtex_view)
        self.copy_bibtex = QLabel("<a href='#' style='text-decoration:none'>Copy BibTeX entry</a>")
        self.copy_bibtex.setObjectName("linkLabel")
        self.copy_bibtex.linkActivated.connect(lambda _l: self._copy(self._bibtex, "BibTeX entry"))
        cv.addWidget(self.copy_bibtex)
        v.addSpacing(8)
        v.addWidget(self.cite_card)

        # projects
        self.folders = QWidget()
        self.folders_layout = FlowLayout(self.folders)
        self.folders.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
        section("In projects", self.folders)
        self.add_project = QToolButton()
        self.add_project.setObjectName("addChip")
        self.add_project.setText("+ Add to project")
        self.add_project.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.project_menu = QMenu(self.add_project)
        self.add_project.setMenu(self.project_menu)
        self._all_projects: list[tuple[int, str, int]] = []

        # files
        self.file_row = QFrame()
        self.file_row.setObjectName("fileRow")
        files_row = QHBoxLayout(self.file_row)
        files_row.setContentsMargins(10, 8, 8, 8)
        files_row.setSpacing(8)
        self.file_icon = QLabel()
        files_row.addWidget(self.file_icon)
        names = QVBoxLayout()
        names.setSpacing(0)
        self.file_link = QLabel()
        self.file_link.setObjectName("linkLabel")
        self.file_link.setCursor(Qt.CursorShape.PointingHandCursor)
        self.file_link.installEventFilter(self)
        self.file_folder = _label("")
        names.addWidget(self.file_link)
        names.addWidget(self.file_folder)
        files_row.addLayout(names, 1)
        self.reveal_button = QToolButton()
        self.reveal_button.setObjectName("iconButton")
        self.reveal_button.setToolTip("Show in folder")
        self.reveal_button.clicked.connect(self.revealFileRequested)
        files_row.addWidget(self.reveal_button)
        self.open_button = QPushButton("Open")
        self.open_button.clicked.connect(self.openFileRequested)
        files_row.addWidget(self.open_button)
        files_box = QWidget()
        files_layout = QVBoxLayout(files_box)
        files_layout.setContentsMargins(0, 0, 0, 0)
        files_layout.setSpacing(4)
        files_layout.addWidget(self.file_row)
        self.supplements = QWidget()
        self.supplements_layout = QVBoxLayout(self.supplements)
        self.supplements_layout.setContentsMargins(4, 0, 0, 0)
        self.supplements_layout.setSpacing(2)
        files_layout.addWidget(self.supplements)
        self.add_supplements = QLabel("<a href='#' style='text-decoration:none'>+ Add supplementary file</a>")
        self.add_supplements.setObjectName("linkLabel")
        self.add_supplements.setToolTip(
            "Attach supplementary information, data, videos, ... to this reference (or drop the files onto its row)"
        )
        self.add_supplements.setCursor(Qt.CursorShape.PointingHandCursor)
        self.add_supplements.installEventFilter(self)
        files_layout.addWidget(self.add_supplements)
        section("Files", files_box)

        # notes
        self.notes = QTextEdit()
        self.notes.setObjectName("notes")
        self.notes.setAcceptRichText(False)
        self.notes.setPlaceholderText("Write a note (Markdown and LaTeX are kept as typed)...")
        self.notes.setMinimumHeight(90)
        self.notes.setMaximumHeight(220)
        section("Notes", self.notes)

        # more details
        self.abstract = InlineTextEdit("abstract", multiline=True, placeholder="Add an abstract (Ctrl+Enter to save)")
        section("Abstract", self.abstract)
        self.tags = InlineTextEdit("tags", placeholder="tag; another tag")
        section("Tags", self.tags)
        self.keywords = InlineTextEdit("keywords", placeholder="keyword; another keyword")
        section("Author keywords", self.keywords)
        self.citation_key = InlineLineEdit("citation_key")
        self.citation_key.setToolTip("Citation key = file name of the PDF. Change it here to rename both.")
        section("Citation key", self.citation_key)
        self.publisher = InlineLineEdit("publisher")
        section("Publisher", self.publisher)
        self.url = InlineLineEdit("url", placeholder="Add URL...")
        self.url_action = self.url.addAction(icons.open_link(), QLineEdit.ActionPosition.TrailingPosition)
        self.url_action.setToolTip("Open in browser")
        self.url_action.triggered.connect(lambda: self.openUrlRequested.emit(self.url.value()))
        section("URL", self.url)

        section("Identifiers")
        ids = QFormLayout()
        ids.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        ids.setHorizontalSpacing(12)
        ids.setVerticalSpacing(1)
        self.arxiv_id = InlineLineEdit("arxiv_id")
        self.doi = InlineLineEdit("doi")
        lookup_actions: dict[str, Any] = {}
        for edit, kind in ((self.arxiv_id, "arxiv"), (self.doi, "doi")):
            action = edit.addAction(icons.search(), QLineEdit.ActionPosition.TrailingPosition)
            lookup_actions[kind] = action
            action.setToolTip("Look up the metadata with this identifier")
            action.triggered.connect(lambda _=False, k=kind: self.lookupRequested.emit(k))
        self.lookup_actions = lookup_actions
        self.isbn = InlineLineEdit("isbn")
        self.issn = InlineLineEdit("issn")
        self.pmid = InlineLineEdit("pmid")
        for caption, widget in (
            ("arXiv ID", self.arxiv_id),
            ("DOI", self.doi),
            ("ISBN", self.isbn),
            ("ISSN", self.issn),
            ("PMID", self.pmid),
        ):
            ids.addRow(_label(caption), widget)
        v.addLayout(ids)

        # outline of the PDF
        self.contents_label = _label("")
        self.contents_label.setWordWrap(True)
        section("Outline", self.contents_label)
        self.outline = QTreeWidget()
        self.outline.setObjectName("outline")
        self.outline.setColumnCount(2)
        self.outline.setHeaderHidden(True)
        self.outline.setMaximumHeight(240)
        self.outline.itemDoubleClicked.connect(lambda *_: self.openFileRequested.emit())
        v.addWidget(self.outline)
        v.addStretch(1)

        self.editors: list[InlineLineEdit | InlineTextEdit] = [
            self.title, self.authors, self.journal, self.year, self.volume, self.issue, self.pages,
            self.abstract, self.tags, self.keywords, self.citation_key, self.publisher, self.url,
            self.arxiv_id, self.doi, self.isbn, self.issn, self.pmid,
        ]  # fmt: skip
        for editor in self.editors:
            editor.committed.connect(self.fieldEdited)
        # a lookup button for code written against 0.7 (the sheet's action bar has the visible one)
        self.lookup_button = QPushButton("Look up metadata")
        self.lookup_button.hide()

    # ------------------------------------------------------------------ population
    def show_message(self, text: str) -> None:
        self.empty.setText(text)
        self.setCurrentIndex(0)

    def set_paper(
        self,
        paper: Paper,
        folders: Sequence[Project] | Sequence[str],
        pdf_name: str,
        attachments: Sequence[Attachment] = (),
        available: dict[int, bool] | None = None,
        *,
        bibtex: str = "",
        bib_name: str = "",
        all_projects: Sequence[tuple[int, str, int]] = (),
    ) -> None:
        m = paper.meta
        self._paper = paper
        self.verify.setVisible(paper.needs_review)
        if paper.needs_review:
            if m.doi:
                found = f"Matched by its DOI ({m.doi}). Check the details, then mark it as verified."
            elif m.arxiv_id:
                found = f"Matched by its arXiv ID ({m.arxiv_id}). Check the details, then mark it as verified."
            else:
                found = ("No DOI or arXiv ID was found in the PDF: the details come from a title search or from the "
                         "PDF itself. Check them, or look the reference up again.")  # fmt: skip
            self.verify_text.setText(found)
        index = self.type_combo.findData(m.entry_type)
        if index < 0:
            self.type_combo.addItem(m.entry_type.upper(), m.entry_type)
            index = self.type_combo.count() - 1
        self.type_combo.setCurrentIndex(index)
        self.star_button.setIcon(icons.star(paper.favorite))
        self.star_button.setToolTip("Starred (click to remove the star)" if paper.favorite else "Star")
        values = {
            "title": m.title,
            "authors": m.authors_inverted(),
            "journal": m.journal,
            "year": str(m.year) if m.year is not None else "",
            "volume": m.volume,
            "issue": m.issue,
            "pages": m.pages,
            "abstract": m.abstract,
            "tags": paper.tags,
            "keywords": m.keywords,
            "citation_key": paper.citation_key,
            "publisher": m.publisher,
            "url": m.url,
            "arxiv_id": m.arxiv_id,
            "doi": m.doi,
            "isbn": m.isbn,
            "issn": m.issn,
            "pmid": m.pmid,
        }
        for editor in self.editors:
            editor.set_value(values[editor.field])
        self._doi = m.doi
        self.doi_link.setText(f"<a href='#' style='text-decoration:none'>doi:{escape(m.doi)} ↗</a>")
        self.doi_link.setVisible(bool(m.doi))
        self._bibtex = bibtex
        self._update_cite()
        self.bib_caption.setText(
            f"As written to {bib_name} — protected words in braces are highlighted"
            if bib_name
            else "As written to the .bib files — protected words in braces are highlighted"
        )
        self.bibtex_view.setText(highlight_bibtex(bibtex) if bibtex else "")
        self.bibtex_view.setVisible(bool(bibtex))
        self.copy_bibtex.setVisible(bool(bibtex))
        self._all_projects = list(all_projects)
        self._set_chips(list(folders))
        self._has_pdf = paper.has_pdf
        if paper.has_pdf:
            suffix = " <span style='opacity:0.65'>(linked)</span>" if paper.pdf_linked else ""
            self.file_link.setText(f"<a href='#' style='text-decoration:none'>{escape(pdf_name)}</a>{suffix}")
            recorded = (paper.relative_pdf_path or "").replace("\\", "/")
            folder = recorded.rsplit("/", 1)[0] + "/" if "/" in recorded else ""
            self.file_folder.setText(folder)
            self.file_link.setToolTip(
                f"Linked file (left in place, never renamed): {recorded}" if paper.pdf_linked else recorded
            )
        else:
            self.file_link.setText("No PDF · <a href='#' style='text-decoration:none'>Attach PDF...</a>")
            recorded = paper.meta.extra_fields.get("file", "")
            self.file_folder.setText("")
            self.file_link.setToolTip(
                "Choose a PDF for this reference, or drop one onto its row in the list."
                + (f"\nRecorded at import but not found: {recorded}" if recorded else "")
            )
        self.file_icon.setVisible(paper.has_pdf)
        self.reveal_button.setVisible(paper.has_pdf)
        self.open_button.setVisible(paper.has_pdf)
        self.set_attachments(attachments, available)
        self.setCurrentIndex(1)

    def citation_command(self) -> str:
        return CITE_COMMANDS[max(0, self.cite_group.checkedId())]

    def citation_text(self) -> str:
        key = self._paper.citation_key if self._paper is not None else ""
        return f"\\{self.citation_command()}{{{key}}}"

    def _update_cite(self) -> None:
        self.cite_code.setText(self.citation_text())

    def copy_citation(self) -> None:
        self._copy(self.citation_text(), "citation")

    def _copy(self, text: str, what: str) -> None:
        if not text:
            return
        QGuiApplication.clipboard().setText(text)
        self.citeCopied.emit(f"Copied {text}" if what == "citation" else f"Copied the {what}")

    def set_attachments(self, attachments: Sequence[Attachment], available: dict[int, bool] | None = None) -> None:
        """One row per supplementary file: icon, name (click opens it) and a menu of actions."""
        for row in self._attachment_rows:
            self.supplements_layout.removeWidget(row)
            row.deleteLater()
        self._attachment_rows = []
        for attachment in attachments:
            row = QWidget()
            row.setObjectName("supplementRow")
            layout = QHBoxLayout(row)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.setSpacing(6)
            icon = QLabel()
            icon.setPixmap((icons.pdf_file() if attachment.is_pdf else icons.paperclip()).pixmap(16, 16))
            layout.addWidget(icon)
            here = True if available is None else available.get(attachment.id, True)
            notes = []
            if attachment.linked:
                notes.append("linked")
            if not here:
                notes.append("not on this device")
            suffix = f" <span style='opacity:0.65'>({', '.join(notes)})</span>" if notes else ""
            name = QLabel(f"<a href='#' style='text-decoration:none'>{escape(attachment.display_name)}</a>{suffix}")
            name.setObjectName("linkLabel")
            name.setToolTip(f"{attachment.file_name}\n{attachment.relative_path}")
            name.setCursor(Qt.CursorShape.PointingHandCursor)
            name.linkActivated.connect(lambda _link, aid=attachment.id: self.attachmentAction.emit("open", aid))
            layout.addWidget(name, 1)
            more = QToolButton()
            more.setObjectName("iconButton")
            more.setText("⋯")
            more.setToolTip("More actions for this file")
            more.setAutoRaise(True)
            more.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
            more.setStyleSheet("QToolButton::menu-indicator { image: none; width: 0px; }")
            menu = QMenu(more)
            for text, action, enabled in (
                ("Open", "open", here),
                ("Show in Folder", "reveal", here),
                ("Rename...", "rename", True),
                ("Make It the Main PDF", "promote", attachment.is_pdf and here),
                ("Remove...", "remove", True),
            ):
                item = menu.addAction(text)
                item.setEnabled(enabled)
                item.triggered.connect(lambda _c=False, a=action, aid=attachment.id: self.attachmentAction.emit(a, aid))
            more.setMenu(menu)
            layout.addWidget(more)
            self.supplements_layout.addWidget(row)
            self._attachment_rows.append(row)
        self.supplements.setVisible(bool(attachments))

    def _set_chips(self, projects: list[Any]) -> None:
        while (item := self.folders_layout.takeAt(0)) is not None:
            if (widget := item.widget()) is not None and widget is not self.add_project:
                widget.deleteLater()
        member: set[int] = set()
        for entry in projects:
            cid, name = (entry if isinstance(entry, tuple) else (None, entry))
            chip = QFrame()
            chip.setObjectName("projectChip")
            row = QHBoxLayout(chip)
            row.setContentsMargins(8, 3, 4, 3)
            row.setSpacing(2)
            row.addWidget(QLabel(name))
            if cid is not None:
                member.add(cid)
                remove = QToolButton()
                remove.setIcon(icons.close_small())
                remove.setIconSize(QSize(12, 12))
                remove.setToolTip(f"Remove from {name}")
                remove.setAccessibleName(f"Remove from {name}")
                remove.clicked.connect(lambda _c=False, c=cid: self.projectToggled.emit(c, False))
                row.addWidget(remove)
            self.folders_layout.addWidget(chip)
        self.project_menu.clear()
        for cid, name, depth in self._all_projects:
            if cid in member:
                continue
            action = self.project_menu.addAction("    " * depth + name)
            action.triggered.connect(lambda _c=False, c=cid: self.projectToggled.emit(c, True))
        if self.project_menu.isEmpty():
            self.project_menu.addAction("(in every project)" if self._all_projects else "(no projects yet)").setEnabled(
                False
            )
        self.project_menu.addSeparator()
        new_project = self.project_menu.addAction("New Project...")
        new_project.triggered.connect(lambda _c=False: self.newProjectRequested.emit())
        self.folders_layout.addWidget(self.add_project)
        self.folders.updateGeometry()

    def refresh_icons(self) -> None:
        self.url_action.setIcon(icons.open_link())
        for action in self.lookup_actions.values():
            action.setIcon(icons.search())
        self.file_icon.setPixmap(icons.pdf_file().pixmap(18, 18))
        self.reveal_button.setIcon(icons.open_link())
        if self._paper is not None:
            self.star_button.setIcon(icons.star(self._paper.favorite))
            self.bibtex_view.setText(highlight_bibtex(self._bibtex) if self._bibtex else "")

    def _on_type_activated(self, index: int) -> None:
        self.fieldEdited.emit("entry_type", self.type_combo.itemData(index))

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if watched is self.file_link and event.type() == QEvent.Type.MouseButtonRelease:
            (self.openFileRequested if self._has_pdf else self.attachFileRequested).emit()
            return True
        if watched is getattr(self, "add_supplements", None) and event.type() == QEvent.Type.MouseButtonRelease:
            self.addSupplementsRequested.emit()
            return True
        return super().eventFilter(watched, event)


# --------------------------------------------------------------------------- the sheet
class ReferenceSheet(QWidget):
    fieldEdited = Signal(str, str)
    notesEdited = Signal(int, str)  # paper id, text
    lookupRequested = Signal(str)
    openFileRequested = Signal()
    attachFileRequested = Signal()
    openUrlRequested = Signal(str)
    revealFileRequested = Signal()
    markReviewedRequested = Signal()
    attachmentAction = Signal(str, int)
    addSupplementsRequested = Signal()
    projectToggled = Signal(int, bool)
    newProjectRequested = Signal()
    favoriteToggled = Signal()
    citeCopied = Signal(str)
    studyRequested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("sheet")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.details = DetailsTab()
        layout.addWidget(self.details, 1)
        self.actions = QWidget()
        self.actions.setObjectName("sheetActions")
        row = QHBoxLayout(self.actions)
        row.setContentsMargins(14, 10, 14, 12)
        row.setSpacing(8)
        self.study_button = QPushButton("Study and ask AI")
        self.study_button.setObjectName("strong")
        self.study_button.setToolTip("Notes and questions to an AI model about this paper (Ctrl+J)")
        self.study_button.clicked.connect(self.studyRequested)
        self.lookup_button = QPushButton("Look up metadata")
        self.lookup_button.setToolTip("Fetch the details from Crossref, arXiv or OpenAlex (DOI, arXiv ID or title)")
        self.lookup_button.clicked.connect(lambda: self.lookupRequested.emit("auto"))
        row.addWidget(self.study_button, 1)
        row.addWidget(self.lookup_button)
        layout.addWidget(self.actions)

        d = self.details
        self.notes = d.notes
        self.outline = d.outline
        self.contents_label = d.contents_label
        for signal in ("fieldEdited", "lookupRequested", "openFileRequested", "attachFileRequested",
                       "openUrlRequested", "markReviewedRequested", "revealFileRequested", "attachmentAction",
                       "addSupplementsRequested", "projectToggled", "newProjectRequested", "favoriteToggled",
                       "citeCopied"):  # fmt: skip
            getattr(d, signal).connect(getattr(self, signal))

        self.paper_id: int | None = None
        self._notes_timer = QTimer(self)
        self._notes_timer.setSingleShot(True)
        self._notes_timer.setInterval(700)
        self._notes_timer.timeout.connect(self.flush_notes)
        self._loading = False
        self._notes_dirty = False
        self.notes.textChanged.connect(self._on_notes_changed)
        self.clear()

    def refresh_icons(self) -> None:
        self.details.refresh_icons()
        self.study_button.setIcon(icons.study())

    def clear(self, message: str = "No reference selected") -> None:
        self.flush_notes()
        self.paper_id = None
        self.details.show_message(message)
        self._loading = True
        self.notes.clear()
        self._loading = False
        self.notes.setEnabled(False)
        self.contents_label.setText("")
        self.outline.clear()
        self.actions.setVisible(False)

    def set_paper(
        self,
        paper: Paper,
        folders: Sequence[Project] | Sequence[str],
        pdf_name: str,
        attachments: Sequence[Attachment] = (),
        available: dict[int, bool] | None = None,
        *,
        bibtex: str = "",
        bib_name: str = "",
        all_projects: Sequence[tuple[int, str, int]] = (),
    ) -> None:
        if self.paper_id != paper.id:
            self.flush_notes()
            self.outline.clear()
            self.contents_label.setText(f"{pdf_name}\nReading the table of contents..." if pdf_name else "")
            self.details.scroll.verticalScrollBar().setValue(0)
        self.paper_id = paper.id
        self.details.set_paper(paper, folders, pdf_name, attachments, available, bibtex=bibtex, bib_name=bib_name,
                               all_projects=all_projects)  # fmt: skip
        self.actions.setVisible(True)
        self.notes.setEnabled(True)
        if self.notes.toPlainText() != paper.notes and not self.notes.hasFocus():
            self._loading = True
            self.notes.setPlainText(paper.notes)
            self._loading = False

    def set_outline(self, paper_id: int, pdf_name: str, outline: PdfOutline | None, error: str = "") -> None:
        if paper_id != self.paper_id:
            return
        self.outline.clear()
        self.outline.setVisible(False)
        if outline is None:
            self.contents_label.setText(f"{pdf_name}\n{error or 'The PDF could not be read.'}")
            return
        pages = f"{outline.page_count} page{'s' if outline.page_count != 1 else ''}"
        if not outline.toc:
            self.contents_label.setText(f"{pdf_name} — {pages}\nThis PDF has no table of contents.")
            return
        self.contents_label.setText(f"{pdf_name} — {pages}")
        parents: dict[int, Any] = {0: self.outline.invisibleRootItem()}
        for level, title, page in outline.toc:
            parent = parents.get(level - 1, self.outline.invisibleRootItem())
            item = QTreeWidgetItem(parent, [title, str(page)])
            item.setTextAlignment(1, Qt.AlignmentFlag.AlignRight)
            parents[level] = item
        self.outline.expandToDepth(0)
        self.outline.resizeColumnToContents(1)
        self.outline.setVisible(True)

    def _on_notes_changed(self) -> None:
        if not self._loading and self.paper_id is not None:
            self._notes_dirty = True
            self._notes_timer.start()

    def flush_notes(self) -> None:
        """Save pending notes now (debounce timeout, selection change, shutdown)."""
        self._notes_timer.stop()
        if self._notes_dirty and self.paper_id is not None:
            self._notes_dirty = False
            self.notesEdited.emit(self.paper_id, self.notes.toPlainText())

    def reload_notes(self, text: str) -> None:
        """Show notes changed elsewhere (Study mode, Ask AI) unless the user is typing here."""
        if self.notes.toPlainText() != text and not self.notes.hasFocus():
            self._loading = True
            self.notes.setPlainText(text)
            self._loading = False


# Name used by code written against 0.7.
InspectorPanel = ReferenceSheet
