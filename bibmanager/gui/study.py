"""Study mode: one reference at a time, with its notes on the left and the Ask AI conversation on the right.

Opened from the reference sheet ("Study and ask AI", Ctrl+J); Esc or Close returns to the list. The notes are the
reference's own notes (the same text as in the sheet), written in Markdown and shown rendered in Preview.
"""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt, QTimer, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QButtonGroup,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QStackedWidget,
    QTextBrowser,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ..core.models import Paper
from . import icons
from .assistant_panel import AssistantPanel


class StudyView(QWidget):
    closeRequested = Signal()
    openPdfRequested = Signal()
    notesEdited = Signal(int, str)  # paper id, text

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("study")
        self.paper: Paper | None = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        bar = QFrame()
        bar.setObjectName("studyBar")
        row = QHBoxLayout(bar)
        row.setContentsMargins(18, 10, 18, 10)
        row.setSpacing(12)
        self.back_button = QPushButton()
        self.back_button.setToolTip("Back to the list (Esc)")
        self.back_button.clicked.connect(self.closeRequested)
        row.addWidget(self.back_button)
        self.key_badge = QLabel()
        self.key_badge.setObjectName("keyBadgeStrong")
        row.addWidget(self.key_badge)
        caption = QLabel("Study")
        row.addWidget(caption)
        row.addStretch(1)
        self.open_button = QPushButton("Open PDF")
        self.open_button.clicked.connect(self.openPdfRequested)
        row.addWidget(self.open_button)
        self.close_button = QPushButton("Close   Esc")
        self.close_button.clicked.connect(self.closeRequested)
        row.addWidget(self.close_button)
        layout.addWidget(bar)

        split = QSplitter(Qt.Orientation.Horizontal)
        split.setChildrenCollapsible(False)
        left = QWidget()
        lv = QVBoxLayout(left)
        lv.setContentsMargins(24, 20, 8, 24)
        lv.setSpacing(6)
        self.title = QLabel()
        self.title.setObjectName("studyTitle")
        self.title.setWordWrap(True)
        lv.addWidget(self.title)
        self.byline = QLabel()
        self.byline.setObjectName("listSubtitle")
        self.byline.setWordWrap(True)
        lv.addWidget(self.byline)
        tabs = QHBoxLayout()
        tabs.setContentsMargins(0, 12, 0, 4)
        notes_label = QLabel("NOTES · MARKDOWN AND LATEX")
        notes_label.setObjectName("sheetKind")
        tabs.addWidget(notes_label)
        tabs.addStretch(1)
        tabs.setSpacing(0)
        self.mode = QButtonGroup(self)
        for i, text in enumerate(("Write", "Preview")):
            button = QToolButton()
            button.setObjectName("segment")
            button.setProperty("first", i == 0)
            button.setProperty("last", i == 1)
            button.setText(text)
            button.setCheckable(True)
            button.setChecked(i == 0)
            self.mode.addButton(button, i)
            tabs.addWidget(button)
        self.mode.idClicked.connect(self._set_mode)
        lv.addLayout(tabs)
        self.pages = QStackedWidget()
        self.editor = QPlainTextEdit()
        self.editor.setObjectName("studyNotes")
        self.editor.setPlaceholderText("Write your notes on this paper...")
        self.preview = QTextBrowser()
        self.preview.setObjectName("studyPreview")
        self.preview.setOpenExternalLinks(True)
        self.pages.addWidget(self.editor)
        self.pages.addWidget(self.preview)
        lv.addWidget(self.pages, 1)
        split.addWidget(left)

        right = QWidget()
        rv = QVBoxLayout(right)
        rv.setContentsMargins(8, 20, 24, 24)
        self.assistant = AssistantPanel()
        rv.addWidget(self.assistant)
        split.addWidget(right)
        split.setSizes([620, 760])
        layout.addWidget(split, 1)

        self._loading = False
        self._dirty = False
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(700)
        self._timer.timeout.connect(self.flush_notes)
        self.editor.textChanged.connect(self._on_text_changed)
        # Save to Notes appends to the stored notes: store what was typed here first
        self.assistant.save_button.pressed.connect(self.flush_notes)
        self.escape = QShortcut(QKeySequence(Qt.Key.Key_Escape), self)
        self.escape.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self.escape.activated.connect(self.closeRequested)
        self.refresh_icons()

    def refresh_icons(self) -> None:
        self.back_button.setIcon(icons.back())
        self.back_button.setIconSize(QSize(16, 16))
        self.assistant.refresh_icons()

    def set_context(self, name: str) -> None:
        self.back_button.setText(f"  {name}")

    def set_paper(self, paper: Paper | None) -> None:
        if self.paper is not None and (paper is None or paper.id != self.paper.id):
            self.flush_notes()
        self.paper = paper
        self.assistant.set_paper(paper)
        if paper is None:
            return
        m = paper.meta
        self.key_badge.setText(paper.citation_key)
        self.title.setText(m.title or "(untitled)")
        parts = [m.authors_inverted(), m.journal, str(m.year) if m.year else ""]
        self.byline.setText(" · ".join(p for p in parts if p))
        self.open_button.setEnabled(paper.has_pdf or bool(m.doi or m.url))
        self.reload_notes(paper.notes)

    def reload_notes(self, text: str, *, force: bool = False) -> None:
        """Show notes changed elsewhere; unless ``force``, not while the user is typing here."""
        if self.editor.toPlainText() != text and (force or not self.editor.hasFocus()):
            self._loading = True
            self.editor.setPlainText(text)
            self._loading = False
        self.preview.setMarkdown(text)

    def _set_mode(self, index: int) -> None:
        if index == 1:
            self.flush_notes()
            self.preview.setMarkdown(self.editor.toPlainText())
        self.pages.setCurrentIndex(index)

    def _on_text_changed(self) -> None:
        if not self._loading and self.paper is not None:
            self._dirty = True
            self._timer.start()

    def flush_notes(self) -> None:
        self._timer.stop()
        if self._dirty and self.paper is not None:
            self._dirty = False
            self.notesEdited.emit(self.paper.id, self.editor.toPlainText())
