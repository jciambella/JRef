"""Right pane: Details / Notes / Contents tabs with click-to-edit fields."""

from __future__ import annotations

from collections.abc import Sequence
from html import escape
from typing import Any

from PySide6.QtCore import QEvent, QObject, QPoint, QRect, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QFocusEvent, QKeyEvent, QResizeEvent
from PySide6.QtWidgets import (
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
    QTabWidget,
    QTextEdit,
    QToolButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..core.extractor import PdfOutline
from ..core.models import Attachment, Paper
from . import icons
from .assistant_panel import AssistantPanel

ENTRY_TYPES: tuple[tuple[str, str], ...] = (
    ("article", "Journal Article"),
    ("book", "Book"),
    ("incollection", "Book Section"),
    ("inproceedings", "Conference Proceedings"),
    ("phdthesis", "Thesis"),
    ("techreport", "Report"),
    ("misc", "Generic"),
)


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
    """Left-to-right layout that wraps its items (folder chips)."""

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


# --------------------------------------------------------------------------- Details tab
class DetailsTab(QStackedWidget):
    fieldEdited = Signal(str, str)
    lookupRequested = Signal(str)  # "auto" | "doi" | "arxiv"
    openFileRequested = Signal()
    attachFileRequested = Signal()
    openUrlRequested = Signal(str)
    revealFileRequested = Signal()
    markReviewedRequested = Signal()
    attachmentAction = Signal(str, int)  # "open" | "reveal" | "rename" | "promote" | "remove", attachment id
    addSupplementsRequested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._has_pdf = True
        self._attachment_rows: list[QWidget] = []
        self.setObjectName("details")
        self.empty = QLabel("No document selected")
        self.empty.setObjectName("multiSelection")
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.addWidget(self.empty)

        body = QWidget()
        body.setObjectName("detailsBody")
        scroll = QScrollArea()
        scroll.setObjectName("detailsScroll")
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        body.setMinimumWidth(0)
        scroll.setWidget(body)
        self.addWidget(scroll)

        v = QVBoxLayout(body)
        v.setContentsMargins(14, 12, 14, 14)
        v.setSpacing(4)

        # "needs review" info bar
        self.banner = QFrame()
        self.banner.setObjectName("reviewBanner")
        banner_layout = QHBoxLayout(self.banner)
        banner_layout.setContentsMargins(10, 6, 6, 6)
        self.banner_icon = QLabel()
        banner_layout.addWidget(self.banner_icon)
        banner_text = QLabel("Are these details correct?")
        banner_text.setStyleSheet("font-weight: 600;")
        banner_layout.addWidget(banner_text)
        banner_layout.addStretch(1)
        reviewed = QPushButton("Details are correct")
        reviewed.clicked.connect(self.markReviewedRequested)
        banner_layout.addWidget(reviewed)
        v.addWidget(self.banner)
        v.addSpacing(4)

        self.title = InlineTextEdit("title", placeholder="Title")
        v.addWidget(self.title)
        self.authors = InlineTextEdit("authors", placeholder="Authors (Surname, Given; Surname, Given)")
        v.addWidget(self.authors)
        v.addSpacing(6)

        self.type_combo = QComboBox()
        self.type_combo.setObjectName("typeCombo")
        self.type_combo.setToolTip("Document type (BibTeX entry type)")
        self.type_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.type_combo.setMinimumContentsLength(12)
        for key, label in ENTRY_TYPES:
            self.type_combo.addItem(label, key)
        self.type_combo.activated.connect(self._on_type_activated)
        type_row = QHBoxLayout()
        type_row.addWidget(self.type_combo)
        type_row.addStretch(1)
        v.addLayout(type_row)
        self.lookup_button = QPushButton("Look up details online")
        self.lookup_button.setObjectName("subtle")
        self.lookup_button.setToolTip("Fetch metadata from Crossref, arXiv or OpenAlex (DOI, arXiv ID or title)")
        self.lookup_button.clicked.connect(lambda: self.lookupRequested.emit("auto"))
        v.addWidget(self.lookup_button)
        v.addSpacing(6)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        form.setHorizontalSpacing(12)
        form.setVerticalSpacing(1)
        self.journal = InlineLineEdit("journal", placeholder="Journal")
        self.year = InlineLineEdit("year", placeholder="Unknown")
        self.volume = InlineLineEdit("volume")
        self.issue = InlineLineEdit("issue")
        self.pages = InlineLineEdit("pages")
        for caption, widget in (
            ("Journal", self.journal),
            ("Year", self.year),
            ("Volume", self.volume),
            ("Issue", self.issue),
            ("Pages", self.pages),
        ):
            form.addRow(_label(caption), widget)
        v.addLayout(form)

        def section(title: str, widget: QWidget) -> None:
            v.addWidget(_label(title, "sectionLabel"))
            v.addWidget(widget)

        files = QWidget()
        files_row = QHBoxLayout(files)
        files_row.setContentsMargins(0, 2, 0, 0)
        files_row.setSpacing(6)
        self.file_icon = QLabel()
        files_row.addWidget(self.file_icon)
        self.file_link = QLabel()
        self.file_link.setObjectName("linkLabel")
        self.file_link.setCursor(Qt.CursorShape.PointingHandCursor)
        self.file_link.installEventFilter(self)
        files_row.addWidget(self.file_link, 1)
        self.reveal_button = QPushButton()
        self.reveal_button.setObjectName("subtle")
        self.reveal_button.setToolTip("Show in folder")
        self.reveal_button.clicked.connect(self.revealFileRequested)
        files_row.addWidget(self.reveal_button)
        files_box = QWidget()
        files_layout = QVBoxLayout(files_box)
        files_layout.setContentsMargins(0, 0, 0, 0)
        files_layout.setSpacing(2)
        files_layout.addWidget(files)
        self.supplements = QWidget()
        self.supplements_layout = QVBoxLayout(self.supplements)
        self.supplements_layout.setContentsMargins(0, 0, 0, 0)
        self.supplements_layout.setSpacing(2)
        files_layout.addWidget(self.supplements)
        self.add_supplements = QLabel("<a href='#' style='text-decoration:none'>Add supplementary files...</a>")
        self.add_supplements.setObjectName("linkLabel")
        self.add_supplements.setToolTip(
            "Attach supplementary information, data, videos, ... to this document (or drop the files onto its row)"
        )
        self.add_supplements.setCursor(Qt.CursorShape.PointingHandCursor)
        self.add_supplements.installEventFilter(self)
        files_layout.addWidget(self.add_supplements)
        section("Files", files_box)

        self.abstract = InlineTextEdit("abstract", multiline=True, placeholder="Add an abstract (Ctrl+Enter to save)")
        section("Abstract", self.abstract)
        self.tags = InlineTextEdit("tags", placeholder="tag; another tag")
        section("Tags", self.tags)
        self.keywords = InlineTextEdit("keywords", placeholder="keyword; another keyword")
        section("Author keywords", self.keywords)
        self.citation_key = InlineLineEdit("citation_key")
        section("Citation key", self.citation_key)
        self.publisher = InlineLineEdit("publisher")
        section("Publisher", self.publisher)
        self.url = InlineLineEdit("url", placeholder="Add URL...")
        self.url_action = self.url.addAction(icons.open_link(), QLineEdit.ActionPosition.TrailingPosition)
        self.url_action.setToolTip("Open in browser")
        self.url_action.triggered.connect(lambda: self.openUrlRequested.emit(self.url.value()))
        section("URL", self.url)
        self.folders = QWidget()
        self.folders_layout = FlowLayout(self.folders)
        self.folders.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
        section("Folders", self.folders)

        v.addWidget(_label("Catalog IDs", "sectionLabel"))
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
            action.setToolTip("Look up details using this identifier")
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
        v.addStretch(1)

        self.editors: list[InlineLineEdit | InlineTextEdit] = [
            self.title, self.authors, self.journal, self.year, self.volume, self.issue, self.pages,
            self.abstract, self.tags, self.keywords, self.citation_key, self.publisher, self.url,
            self.arxiv_id, self.doi, self.isbn, self.issn, self.pmid,
        ]  # fmt: skip
        for editor in self.editors:
            editor.committed.connect(self.fieldEdited)

    # ------------------------------------------------------------------ population
    def show_message(self, text: str) -> None:
        self.empty.setText(text)
        self.setCurrentIndex(0)

    def set_paper(
        self,
        paper: Paper,
        folders: list[str],
        pdf_name: str,
        attachments: Sequence[Attachment] = (),
        available: dict[int, bool] | None = None,
    ) -> None:
        m = paper.meta
        self.banner.setVisible(paper.needs_review)
        index = self.type_combo.findData(m.entry_type)
        if index < 0:
            self.type_combo.addItem(m.entry_type, m.entry_type)
            index = self.type_combo.count() - 1
        self.type_combo.setCurrentIndex(index)
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
        self._set_chips(folders)
        self._has_pdf = paper.has_pdf
        if paper.has_pdf:
            suffix = " <span style='opacity:0.65'>(linked)</span>" if paper.pdf_linked else ""
            self.file_link.setText(f"<a href='#' style='text-decoration:none'>{pdf_name}</a>{suffix}")
            tip = paper.relative_pdf_path or ""
            self.file_link.setToolTip(f"Linked file (left in place, never renamed): {tip}" if paper.pdf_linked else tip)
        else:
            self.file_link.setText("No PDF \u00b7 <a href='#' style='text-decoration:none'>Attach PDF...</a>")
            recorded = paper.meta.extra_fields.get("file", "")
            self.file_link.setToolTip(
                "Choose a PDF for this reference, or drop one onto its row in the list."
                + (f"\nRecorded at import but not found: {recorded}" if recorded else "")
            )
        self.file_icon.setVisible(paper.has_pdf)
        self.reveal_button.setVisible(paper.has_pdf)
        self.set_attachments(attachments, available)
        self.setCurrentIndex(1)

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
            icon.setPixmap((icons.pdf_file() if attachment.is_pdf else icons.paperclip()).pixmap(18, 18))
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
            more.setObjectName("subtle")
            more.setText("\u22ef")
            more.setToolTip("More actions for this file")
            more.setAutoRaise(True)
            more.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
            more.setStyleSheet("QToolButton::menu-indicator { image: none; width: 0px; }")
            menu = QMenu(more)
            for text, action, enabled in (
                ("Open", "open", here),
                ("Show in Folder", "reveal", here),
                ("Rename...", "rename", True),
                ("Set as Main PDF", "promote", attachment.is_pdf and here),
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

    def _set_chips(self, names: list[str]) -> None:
        while (item := self.folders_layout.takeAt(0)) is not None:
            if (widget := item.widget()) is not None:
                widget.deleteLater()
        for name in names or ["Not in any folder"]:
            chip = QLabel(name)
            chip.setObjectName("chip" if names else "fieldLabel")
            self.folders_layout.addWidget(chip)
        self.folders.updateGeometry()

    def refresh_icons(self) -> None:
        self.banner_icon.setPixmap(icons.needs_review().pixmap(18, 18))
        self.lookup_button.setIcon(icons.search())
        self.url_action.setIcon(icons.open_link())
        for action in self.lookup_actions.values():
            action.setIcon(icons.search())
        self.file_icon.setPixmap(icons.pdf_file().pixmap(18, 18))
        self.reveal_button.setIcon(icons.open_link())

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


# --------------------------------------------------------------------------- Inspector
class InspectorPanel(QTabWidget):
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

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("inspector")
        self.setDocumentMode(False)
        self.details = DetailsTab()
        self.notes = QTextEdit()
        self.notes.setObjectName("notes")
        self.notes.setAcceptRichText(False)
        self.notes.setPlaceholderText("Write notes about this document...")
        contents = QWidget()
        contents_layout = QVBoxLayout(contents)
        contents_layout.setContentsMargins(8, 8, 8, 8)
        self.contents_label = _label("")
        self.contents_label.setWordWrap(True)
        self.outline = QTreeWidget()
        self.outline.setObjectName("outline")
        self.outline.setColumnCount(2)
        self.outline.setHeaderHidden(True)
        self.outline.itemDoubleClicked.connect(lambda *_: self.openFileRequested.emit())
        contents_layout.addWidget(self.contents_label)
        contents_layout.addWidget(self.outline, 1)
        self.addTab(self.details, "Details")
        self.addTab(self.notes, "Notes")
        self.addTab(contents, "Contents")
        self.assistant = AssistantPanel()
        self.addTab(self.assistant, "Ask AI")

        self.details.fieldEdited.connect(self.fieldEdited)
        self.details.lookupRequested.connect(self.lookupRequested)
        self.details.openFileRequested.connect(self.openFileRequested)
        self.details.attachFileRequested.connect(self.attachFileRequested)
        self.details.openUrlRequested.connect(self.openUrlRequested)
        self.details.markReviewedRequested.connect(self.markReviewedRequested)
        self.details.revealFileRequested.connect(self.revealFileRequested)
        self.details.attachmentAction.connect(self.attachmentAction)
        self.details.addSupplementsRequested.connect(self.addSupplementsRequested)

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
        self.assistant.refresh_icons()

    def clear(self, message: str = "No document selected") -> None:
        self.flush_notes()
        self.paper_id = None
        self.details.show_message(message)
        self._loading = True
        self.notes.clear()
        self._loading = False
        self.notes.setEnabled(False)
        self.contents_label.setText("")
        self.outline.clear()
        self.assistant.set_paper(None)

    def set_paper(
        self,
        paper: Paper,
        folders: list[str],
        pdf_name: str,
        attachments: Sequence[Attachment] = (),
        available: dict[int, bool] | None = None,
    ) -> None:
        if self.paper_id != paper.id:
            self.flush_notes()
            self.outline.clear()
            self.contents_label.setText(f"{pdf_name}\nReading table of contents...")
        self.paper_id = paper.id
        self.details.set_paper(paper, folders, pdf_name, attachments, available)
        self.assistant.set_paper(paper)
        self.notes.setEnabled(True)
        if self.notes.toPlainText() != paper.notes and not self.notes.hasFocus():
            self._loading = True
            self.notes.setPlainText(paper.notes)
            self._loading = False

    def set_outline(self, paper_id: int, pdf_name: str, outline: PdfOutline | None, error: str = "") -> None:
        if paper_id != self.paper_id:
            return
        self.outline.clear()
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
