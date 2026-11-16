"""Inspector tab "Ask AI": questions about the selected paper to Claude, ChatGPT models, Gemini or a local model.

The provider clients are in ``core/assistant.py`` (pure Python); API keys in ``core/secrets.py``. Reading the PDF and
the HTTP request run in a worker thread; text pieces come back through Qt signals connected to bound slots, so they are
handled on the GUI thread (architecture invariant 4).
"""

from __future__ import annotations

import itertools
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from html import escape
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, Qt, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import QGuiApplication, QKeyEvent
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QTextBrowser,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ..core.assistant import (
    PDF_LIMIT_BYTES,
    PROVIDERS,
    QUICK_QUESTIONS,
    WEB_APPS,
    AssistantClient,
    AssistantError,
    AssistantSettings,
    ChatMessage,
    PaperContext,
    Usage,
    build_context,
    web_prompt,
)
from ..core.extractor import PdfReadError, read_pdf
from ..core.library import Library
from ..core.models import Paper
from ..core.secrets import KeyStore
from . import icons

log = logging.getLogger(__name__)


@dataclass(slots=True)
class Exchange:
    question: str
    answer: str = ""
    error: str = ""
    usage: Usage | None = None
    model: str = ""
    done: bool = False


@dataclass(slots=True)
class Conversation:
    exchanges: list[Exchange] = field(default_factory=list)

    def history(self) -> list[ChatMessage]:
        """Previous complete exchanges, sent with the next question (failed ones are left out)."""
        messages: list[ChatMessage] = []
        for exchange in self.exchanges:
            if exchange.done and exchange.answer and not exchange.error:
                messages += [ChatMessage("user", exchange.question), ChatMessage("assistant", exchange.answer)]
        return messages


# --------------------------------------------------------------------------- worker
class _Bridge(QObject):
    chunk = Signal(int, str)
    finished = Signal(int, object)  # Usage
    failed = Signal(int, str)
    context = Signal(int, object)  # PaperContext
    models = Signal(int, object)  # list[str] | str (error)


@dataclass(slots=True)
class _Job:
    token: int
    settings: AssistantSettings
    key: str
    paper: Paper
    question: str
    history: list[ChatMessage]
    documents: list[tuple[str, Path]]  # (label, path) of the PDFs to read; the main PDF first
    send_pdf: bool
    max_chars: int
    context: PaperContext | None  # cached from a previous question
    cancel: threading.Event


def _run_job(job: _Job, bridge: _Bridge) -> None:
    try:
        context = job.context
        if context is None:
            pages: list[tuple[str, list[str]]] = []
            for label, path in job.documents:
                try:
                    pages.append((label, read_pdf(path).page_texts))
                except PdfReadError as exc:
                    log.warning("%s", exc)
            pdf_bytes = None
            main = job.documents[0][1] if job.documents and job.documents[0][0].startswith("Main") else None
            if job.send_pdf and main is not None:
                data = main.read_bytes()
                limit = PDF_LIMIT_BYTES.get(PROVIDERS[job.settings.provider].api, 0)
                pdf_bytes = data if len(data) <= limit else None
            context = build_context(
                job.paper, pages, max_chars=job.max_chars, pdf_bytes=pdf_bytes, pdf_name=main.name if main else ""
            )
            bridge.context.emit(job.token, context)
        client = AssistantClient(job.settings, job.key)
        try:
            usage = client.ask(
                context, job.history, job.question, lambda text: bridge.chunk.emit(job.token, text), job.cancel.is_set
            )
        finally:
            client.close()
        bridge.finished.emit(job.token, usage)
    except AssistantError as exc:
        bridge.failed.emit(job.token, str(exc))
    except Exception as exc:  # unexpected: shown in the conversation, logged with the traceback
        log.exception("Ask AI failed")
        bridge.failed.emit(job.token, f"{type(exc).__name__}: {exc}")


def _list_models(token: int, settings: AssistantSettings, key: str, bridge: _Bridge) -> None:
    try:
        client = AssistantClient(settings, key)
        try:
            bridge.models.emit(token, client.list_models())
        finally:
            client.close()
    except AssistantError as exc:
        bridge.models.emit(token, str(exc))
    except Exception as exc:
        log.exception("Listing models failed")
        bridge.models.emit(token, f"{type(exc).__name__}: {exc}")


# --------------------------------------------------------------------------- settings dialog
class AssistantSettingsDialog(QDialog):
    """API keys and options. Keys go to the key store (encrypted with the Windows login), never to config.json."""

    def __init__(self, parent: QWidget | None, *, config: Any, keys: KeyStore, provider: str) -> None:
        super().__init__(parent)
        self.setWindowTitle("Ask AI — Settings")
        self.setWindowIcon(icons.app_icon())
        self.setMinimumWidth(620)
        self.config, self.keys = config, keys
        self._keys: dict[str, str] = {}
        self._urls = dict(config.assistant_base_urls)
        layout = QVBoxLayout(self)
        intro = QLabel(
            "<p>Answers come from the provider's <b>API</b>, used with your own API key and billed by the provider "
            "per use. A ChatGPT, Claude or Gemini <i>subscription</i> does not include API use: with a "
            "subscription, use <b>Ask in browser</b> in the panel instead. A local model (Ollama) needs no key and "
            "nothing leaves this computer.</p>"
        )
        intro.setWordWrap(True)
        intro.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(intro)
        form = QFormLayout()
        self.provider = QComboBox()
        for p in PROVIDERS.values():
            self.provider.addItem(p.label, p.id)
        self.provider.setCurrentIndex(max(0, self.provider.findData(provider)))
        form.addRow("Provider:", self.provider)
        self.key = QLineEdit()
        self.key.setEchoMode(QLineEdit.EchoMode.Password)
        self.key_link = QLabel()
        self.key_link.setOpenExternalLinks(True)
        self.key_link.setTextFormat(Qt.TextFormat.RichText)
        key_row = QHBoxLayout()
        key_row.addWidget(self.key, 1)
        key_row.addWidget(self.key_link)
        form.addRow("API key:", key_row)
        self.url = QLineEdit()
        form.addRow("Server address:", self.url)
        self.workspace = QLineEdit(config.assistant_workspace_id)
        self.workspace.setPlaceholderText("Only for Claude keys that are not scoped to one workspace (wrkspc_...)")
        self.workspace_label = QLabel("Claude workspace ID:")
        form.addRow(self.workspace_label, self.workspace)
        self.hint = QLabel()
        self.hint.setWordWrap(True)
        self.hint.setObjectName("fieldLabel")
        form.addRow("", self.hint)
        self.send_pdf = QCheckBox("Send the PDF file itself (figures, tables, equations as printed; more tokens)")
        self.send_pdf.setToolTip("Claude, ChatGPT models and Gemini read PDF files; others always get the text")
        self.send_pdf.setChecked(config.assistant_send_pdf)
        form.addRow("", self.send_pdf)
        self.supplements = QCheckBox("Include the text of supplementary PDFs")
        self.supplements.setChecked(config.assistant_supplements)
        form.addRow("", self.supplements)
        self.max_chars = QSpinBox()
        self.max_chars.setRange(20, 2000)
        self.max_chars.setSingleStep(20)
        self.max_chars.setSuffix(" thousand characters")
        self.max_chars.setValue(config.assistant_max_chars // 1000)
        self.max_chars.setToolTip("About 4 characters per token: 300 thousand characters are about 75 thousand tokens")
        form.addRow("Paper text sent at most:", self.max_chars)
        self.max_tokens = QSpinBox()
        self.max_tokens.setRange(256, 64000)
        self.max_tokens.setSingleStep(512)
        self.max_tokens.setSuffix(" tokens")
        self.max_tokens.setValue(config.assistant_max_tokens)
        form.addRow("Answer length at most:", self.max_tokens)
        layout.addLayout(form)
        self.store_note = QLabel(f"Keys are stored in: {keys.backend}.")
        self.store_note.setObjectName("fieldLabel")
        self.store_note.setWordWrap(True)
        layout.addWidget(self.store_note)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        save = QPushButton("Save")
        save.setObjectName("primary")
        save.setDefault(True)
        save.clicked.connect(self.accept)
        buttons.addWidget(cancel)
        buttons.addWidget(save)
        layout.addLayout(buttons)
        self._current = ""
        self.provider.currentIndexChanged.connect(self._show_provider)
        self._show_provider()

    def _remember(self) -> None:
        if self._current:
            self._keys[self._current] = self.key.text()
            self._urls[self._current] = self.url.text().strip()

    def _show_provider(self, *_args: Any) -> None:
        self._remember()
        pid = str(self.provider.currentData())
        p = PROVIDERS[pid]
        self._current = pid
        self.key.setText(self._keys.get(pid, self.keys.get(pid) if self.keys.is_stored(pid) else ""))
        self.key.setPlaceholderText(
            ("Stored: leave as is" if self.keys.is_stored(pid) else "Paste your API key")
            if p.needs_key
            else "Not needed" if p.local else "Optional"
        )
        self.key.setEnabled(not p.local)
        self.key_link.setText(f"<a href='{p.key_url}'>{'Get Ollama' if p.local else 'Get a key'}</a>" if p.key_url else "")
        self.url.setText(self._urls.get(pid, ""))
        self.url.setPlaceholderText(p.base_url or "https://your-server/v1")
        self.hint.setText(p.hint)
        self.workspace.setVisible(pid == "anthropic")
        self.workspace_label.setVisible(pid == "anthropic")

    def apply(self) -> str:
        """Store keys and options; returns the provider selected in the dialog."""
        self._remember()
        for pid, key in self._keys.items():
            if key.strip() != (self.keys.get(pid) if self.keys.is_stored(pid) else ""):
                self.keys.set(pid, key)
        c = self.config
        c.assistant_base_urls = {pid: url for pid, url in self._urls.items() if url}
        c.assistant_workspace_id = self.workspace.text().strip()
        c.assistant_send_pdf = self.send_pdf.isChecked()
        c.assistant_supplements = self.supplements.isChecked()
        c.assistant_max_chars = self.max_chars.value() * 1000
        c.assistant_max_tokens = self.max_tokens.value()
        return self._current


class _QuestionEdit(QPlainTextEdit):
    submitted = Signal()

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and event.modifiers() & (
            Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.MetaModifier
        ):
            self.submitted.emit()
            return
        super().keyPressEvent(event)


# --------------------------------------------------------------------------- the panel
class AssistantPanel(QWidget):
    paperChanged = Signal(object)  # Paper whose notes were extended
    _tokens = itertools.count(1)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.library: Library | None = None
        self.keys: KeyStore | None = None
        self._save_settings: Callable[[], None] = lambda: None
        self._launch: Callable[[QUrl], Any] = lambda url: None
        self._reveal: Callable[[Path], Any] = lambda path: None
        self.paper: Paper | None = None
        self.conversations: dict[int, Conversation] = {}
        self._contexts: dict[tuple[Any, ...], PaperContext] = {}
        self._job: _Job | None = None
        self._job_paper: int | None = None
        self._job_key: tuple[Any, ...] = ()
        self._models_token = 0
        self.bridge = _Bridge(self)
        self.bridge.chunk.connect(self._on_chunk)
        self.bridge.finished.connect(self._on_finished)
        self.bridge.failed.connect(self._on_failed)
        self.bridge.context.connect(self._on_context)
        self.bridge.models.connect(self._on_models)
        self._render_timer = QTimer(self)
        self._render_timer.setSingleShot(True)
        self._render_timer.setInterval(80)
        self._render_timer.timeout.connect(self._render)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)
        top = QHBoxLayout()
        top.setSpacing(4)
        self.provider = QComboBox()
        self.provider.setToolTip("Who answers")
        for p in PROVIDERS.values():
            self.provider.addItem(p.label, p.id)
        self.provider.currentIndexChanged.connect(self._on_provider_changed)
        top.addWidget(self.provider, 2)
        self.model = QComboBox()
        self.model.setEditable(True)
        self.model.setToolTip("Model (the list comes from the provider; you can also type a model name)")
        self.model.lineEdit().setPlaceholderText("model")
        self.model.currentTextChanged.connect(self._on_model_changed)
        top.addWidget(self.model, 3)
        self.refresh_button = QToolButton()
        self.refresh_button.setObjectName("iconButton")
        self.refresh_button.setToolTip("Get the list of models from the provider")
        self.refresh_button.clicked.connect(self.refresh_models)
        top.addWidget(self.refresh_button)
        self.settings_button = QToolButton()
        self.settings_button.setObjectName("iconButton")
        self.settings_button.setToolTip("API keys and options")
        self.settings_button.clicked.connect(self.open_settings)
        top.addWidget(self.settings_button)
        layout.addLayout(top)

        self.info = QLabel()
        self.info.setObjectName("fieldLabel")
        self.info.setWordWrap(True)
        self.info.setTextFormat(Qt.TextFormat.RichText)
        self.info.linkActivated.connect(self._on_info_link)
        layout.addWidget(self.info)

        self.transcript = QTextBrowser()
        self.transcript.setObjectName("assistantTranscript")
        self.transcript.setFrameShape(QFrame.Shape.NoFrame)
        self.transcript.setOpenExternalLinks(True)
        layout.addWidget(self.transcript, 1)

        quick = QHBoxLayout()
        quick.setSpacing(4)
        self.quick_buttons = []
        for label, question in QUICK_QUESTIONS:
            button = QPushButton(label)
            button.setObjectName("subtle")
            button.setToolTip(question)
            button.clicked.connect(lambda _c=False, q=question: self.ask(q))
            quick.addWidget(button)
            self.quick_buttons.append(button)
        quick.addStretch(1)
        layout.addLayout(quick)

        self.question = _QuestionEdit()
        self.question.setPlaceholderText("Ask a question about this paper (Ctrl+Enter to send)")
        self.question.setFixedHeight(68)
        self.question.submitted.connect(self._send_clicked)
        layout.addWidget(self.question)
        bottom = QHBoxLayout()
        bottom.setSpacing(4)
        self.save_button = QPushButton("Save to Notes")
        self.save_button.setToolTip("Append the last question and answer to the notes of this document")
        self.save_button.clicked.connect(self.save_to_notes)
        self.copy_button = QPushButton("Copy")
        self.copy_button.setToolTip("Copy the last answer")
        self.copy_button.clicked.connect(self.copy_answer)
        self.clear_button = QPushButton("New")
        self.clear_button.setToolTip("Start a new conversation about this document")
        self.clear_button.clicked.connect(self.new_conversation)
        self.web_button = QToolButton()
        self.web_button.setObjectName("sortButton")  # themed like the other menu buttons
        self.web_button.setText("Ask in browser")
        self.web_button.setToolTip(
            "Use your ChatGPT / Claude / Gemini / Copilot subscription: the question is copied and the PDF is shown "
            "in its folder, to drag into the chat"
        )
        self.web_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        menu = QMenu(self.web_button)
        for name, url in WEB_APPS:
            menu.addAction(name).triggered.connect(lambda _c=False, n=name, u=url: self.ask_in_browser(n, u))
        self.web_button.setMenu(menu)
        self.send_button = QPushButton("Ask")
        self.send_button.setObjectName("primary")
        self.send_button.clicked.connect(self._send_clicked)
        for widget in (self.save_button, self.copy_button, self.clear_button, self.web_button):
            bottom.addWidget(widget)
        bottom.addStretch(1)
        bottom.addWidget(self.send_button)
        layout.addLayout(bottom)
        self.refresh_icons()
        self._update_state()

    # ------------------------------------------------------------------ wiring
    def bind(
        self,
        library: Library,
        keys: KeyStore,
        *,
        save_settings: Callable[[], None],
        launch: Callable[[QUrl], Any],
        reveal: Callable[[Path], Any],
    ) -> None:
        self.library, self.keys = library, keys
        self._save_settings, self._launch, self._reveal = save_settings, launch, reveal
        config = library.config
        self.provider.blockSignals(True)
        self.provider.setCurrentIndex(max(0, self.provider.findData(config.assistant_provider)))
        self.provider.blockSignals(False)
        self._show_model()
        self._update_state()

    def refresh_icons(self) -> None:
        self.refresh_button.setIcon(icons.sync())
        self.settings_button.setIcon(icons.settings())

    @property
    def provider_id(self) -> str:
        return str(self.provider.currentData())

    def _settings(self) -> AssistantSettings:
        assert self.library is not None
        c = self.library.config
        return AssistantSettings(
            provider=self.provider_id,
            model=self.model.currentText().strip(),
            base_url=c.assistant_base_urls.get(self.provider_id, ""),
            max_tokens=c.assistant_max_tokens,
            workspace_id=c.assistant_workspace_id,
        )

    def _key(self) -> str:
        return self.keys.get(self.provider_id) if self.keys is not None else ""

    def _show_model(self) -> None:
        if self.library is None:
            return
        self.model.blockSignals(True)
        self.model.clear()
        current = self.library.config.assistant_models.get(self.provider_id, "")
        if current:
            self.model.addItem(current)
        self.model.setCurrentText(current)
        self.model.blockSignals(False)

    def _on_provider_changed(self, *_args: Any) -> None:
        if self.library is None:
            return
        self.library.config.assistant_provider = self.provider_id
        self._save_settings()
        self._show_model()
        self._update_state()
        if not self.model.currentText() and (self._key() or not PROVIDERS[self.provider_id].needs_key):
            self.refresh_models()

    def _on_model_changed(self, text: str) -> None:
        if self.library is None:
            return
        self.library.config.assistant_models[self.provider_id] = text.strip()
        self._save_settings()
        self._update_state()

    def refresh_models(self) -> None:
        if self.library is None:
            return
        self._models_token = next(self._tokens)
        self.info.setText("Getting the list of models...")
        threading.Thread(
            target=_list_models, args=(self._models_token, self._settings(), self._key(), self.bridge), daemon=True
        ).start()

    @Slot(int, object)
    def _on_models(self, token: int, result: object) -> None:
        if token != self._models_token:
            return
        if isinstance(result, str):
            self._update_state(error=result)
            return
        models = list(result)  # type: ignore[call-overload]
        current = self.model.currentText()
        self.model.blockSignals(True)
        self.model.clear()
        self.model.addItems(models)
        self.model.setCurrentText(current if current else (models[0] if models else ""))
        self.model.blockSignals(False)
        self._on_model_changed(self.model.currentText())
        self._update_state(note=f"{len(models)} models available." if models else "The provider listed no models.")

    def open_settings(self) -> None:
        if self.library is None or self.keys is None:
            return
        dialog = AssistantSettingsDialog(self, config=self.library.config, keys=self.keys, provider=self.provider_id)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        provider = dialog.apply()
        self._contexts.clear()  # options that shape the context may have changed
        self._save_settings()
        if provider != self.provider_id:
            self.provider.setCurrentIndex(self.provider.findData(provider))
        else:
            self._update_state()
            if not self.model.currentText():
                self.refresh_models()

    def _on_info_link(self, link: str) -> None:
        if link == "#settings":
            self.open_settings()

    # ------------------------------------------------------------------ paper and state
    def set_paper(self, paper: Paper | None) -> None:
        changed = (paper.id if paper else None) != (self.paper.id if self.paper else None)
        self.paper = paper
        if changed:
            self._render()
        self._update_state()

    def _documents(self) -> list[tuple[str, Path]]:
        """PDFs whose text is sent: the main PDF, then (option) the supplementary PDFs on this computer."""
        assert self.library is not None and self.paper is not None
        documents: list[tuple[str, Path]] = []
        main = self.library.pdf_path(self.paper)
        if main is not None and main.is_file():
            documents.append((f"Main PDF: {main.name}", main))
        if self.library.config.assistant_supplements and self.paper.attachments:
            for attachment in self.library.attachments(self.paper.id):
                path = self.library.attachment_path(attachment)
                if attachment.is_pdf and path.is_file():
                    documents.append((f"Supplementary file: {attachment.display_name}", path))
        return documents

    def _context_key(self, documents: list[tuple[str, Path]]) -> tuple[Any, ...]:
        assert self.library is not None and self.paper is not None
        c = self.library.config
        stamps = tuple((str(p), p.stat().st_mtime) for _label, p in documents)
        send_pdf = c.assistant_send_pdf and PROVIDERS[self.provider_id].pdf_input
        return (self.paper.id, stamps, send_pdf, c.assistant_max_chars, PROVIDERS[self.provider_id].api)

    def _update_state(self, *, error: str = "", note: str = "") -> None:
        busy = self._job is not None
        has_paper = self.paper is not None and self.library is not None
        provider = PROVIDERS[self.provider_id]
        needs_key = provider.needs_key and not self._key()
        self.send_button.setText("Stop" if busy else "Ask")
        self.send_button.setEnabled(has_paper or busy)
        for widget in (*self.quick_buttons, self.question):
            widget.setEnabled(has_paper and not busy)
        conversation = self.conversations.get(self.paper.id) if self.paper else None
        answered = bool(conversation and any(e.answer for e in conversation.exchanges))
        for widget in (self.save_button, self.copy_button, self.clear_button):
            widget.setEnabled(answered and not busy)
        self.web_button.setEnabled(has_paper)
        self.provider.setEnabled(not busy)
        self.model.setEnabled(not busy)
        if error:
            self.info.setText(f"<span style='color:#c42b1c'>{escape(error)}</span> &nbsp;<a href='#settings'>Settings</a>")
            return
        if not has_paper:
            self.info.setText("Select one document to ask questions about it.")
            return
        if needs_key:
            self.info.setText(
                f"No API key for {escape(provider.label)}. <a href='#settings'>Add a key</a> "
                "&nbsp;or use <b>Ask in browser</b> with a subscription."
            )
            return
        assert self.paper is not None and self.library is not None
        documents = self._documents()
        cached = self._contexts.get(self._context_key(documents)) if documents else None
        if not documents:
            what = "No PDF on this computer: only the library record (title, authors, abstract) is sent."
        elif self.library.config.assistant_send_pdf and provider.pdf_input:
            what = f"The PDF file{' and supplementary text' if len(documents) > 1 else ''} is sent with each question."
        elif cached is not None:
            cut = " (cut at the configured length)" if cached.truncated else ""
            what = f"About {cached.estimated_tokens // 1000 or 1}k tokens of paper text{cut} are sent with each question."
        else:
            n = len(documents)
            what = f"The text of {n} PDF{'s' if n != 1 else ''} is sent with each question."
        where = "Stays on this computer." if provider.local else f"Sent to {escape(provider.label)}."
        self.info.setText(f"{what} {where} {escape(note)}".strip())

    # ------------------------------------------------------------------ asking
    def _send_clicked(self) -> None:
        if self._job is not None:
            self._job.cancel.set()
            return
        self.ask(self.question.toPlainText())

    def _acknowledged(self) -> bool:
        """Once per cloud provider: what is sent where, and the warning about confidential manuscripts."""
        assert self.library is not None
        provider = PROVIDERS[self.provider_id]
        config = self.library.config
        if provider.local or provider.id in config.assistant_acknowledged:
            return True
        answer = QMessageBox.question(
            self,
            f"Ask AI — {provider.label}",
            f"The text of the paper (or the PDF file) and your questions are sent to {provider.label} with your API "
            "key, under the provider's terms.\n\n"
            "Do not send confidential documents, such as manuscripts you are reviewing: journals and funders forbid "
            "uploading them to AI services. For those, use a local model (Ollama).\n\nContinue?",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return False
        config.assistant_acknowledged = (*config.assistant_acknowledged, provider.id)
        self._save_settings()
        return True

    def ask(self, question: str) -> None:
        question = question.strip()
        if not question or self.paper is None or self.library is None or self._job is not None:
            return
        provider = PROVIDERS[self.provider_id]
        if provider.needs_key and not self._key():
            self.open_settings()
            if not self._key():
                return
        if not self.model.currentText().strip():
            self._update_state(error="Choose a model first (refresh the list with the button next to it).")
            return
        if not self._acknowledged():
            return
        settings = self._settings()
        documents = self._documents()
        key = self._context_key(documents)
        conversation = self.conversations.setdefault(self.paper.id, Conversation())
        exchange = Exchange(question, model=f"{provider.label} · {settings.model}")
        history = conversation.history()
        conversation.exchanges.append(exchange)
        send_pdf = self.library.config.assistant_send_pdf and provider.pdf_input
        self._job = _Job(
            token=next(self._tokens), settings=settings, key=self._key(), paper=self.paper, question=question,
            history=history, documents=documents, send_pdf=send_pdf, max_chars=self.library.config.assistant_max_chars,
            context=self._contexts.get(key), cancel=threading.Event(),
        )  # fmt: skip
        self._job_paper = self.paper.id
        self._job_key = key
        self.question.clear()
        self._render()
        self._update_state()
        threading.Thread(target=_run_job, args=(self._job, self.bridge), daemon=True).start()

    def _current_exchange(self, token: int) -> Exchange | None:
        if self._job is None or token != self._job.token or self._job_paper is None:
            return None
        conversation = self.conversations.get(self._job_paper)
        return conversation.exchanges[-1] if conversation and conversation.exchanges else None

    @Slot(int, object)
    def _on_context(self, token: int, context: object) -> None:
        if self._job is not None and token == self._job.token:
            self._contexts[self._job_key] = context  # type: ignore[assignment]

    @Slot(int, str)
    def _on_chunk(self, token: int, text: str) -> None:
        exchange = self._current_exchange(token)
        if exchange is not None:
            exchange.answer += text
            if not self._render_timer.isActive():
                self._render_timer.start()

    @Slot(int, object)
    def _on_finished(self, token: int, usage: object) -> None:
        exchange = self._current_exchange(token)
        if exchange is None:
            return
        exchange.usage = usage if isinstance(usage, Usage) else None
        exchange.done = True
        if self._job is not None and self._job.cancel.is_set():
            exchange.answer += "\n\n*[stopped]*"
        self._job = None
        self._render()
        self._update_state()

    @Slot(int, str)
    def _on_failed(self, token: int, message: str) -> None:
        exchange = self._current_exchange(token)
        if exchange is None:
            return
        exchange.error, exchange.done = message, True
        self._job = None
        self._render()
        self._update_state(error=message)

    def _render(self) -> None:
        """The conversation as Markdown (answers are Markdown; LaTeX is shown as written)."""
        conversation = self.conversations.get(self.paper.id) if self.paper else None
        if conversation is None or not conversation.exchanges:
            title = escape(self.paper.meta.title or self.paper.citation_key) if self.paper else ""
            self.transcript.setHtml(
                f"<p style='color:gray'>Ask about <b>{title}</b>: its assumptions, equations, methods, results... "
                "Answers cite pages as (p. N). Check important statements in the paper.</p>"
                if self.paper
                else ""
            )
            return
        parts = []
        for exchange in conversation.exchanges:
            question = exchange.question.replace("\n", "  \n> ")
            parts.append(f"> **You:** {question}\n")
            if exchange.answer:
                parts.append(exchange.answer)
            elif not exchange.error:
                parts.append("*...*")
            if exchange.error:
                parts.append(f"**Error:** {exchange.error}")
            if exchange.done and not exchange.error:
                usage = exchange.usage.describe() if exchange.usage else ""
                parts.append(f"<sub>{escape(exchange.model)}{' · ' + usage if usage else ''}</sub>")
            parts.append("\n---\n")
        bar = self.transcript.verticalScrollBar()
        at_end = bar.value() >= bar.maximum() - 4
        self.transcript.setMarkdown("\n\n".join(parts))
        if at_end or self._job is not None:
            bar.setValue(bar.maximum())

    # ------------------------------------------------------------------ actions on answers
    def _last_answer(self) -> Exchange | None:
        conversation = self.conversations.get(self.paper.id) if self.paper else None
        if conversation is None:
            return None
        return next((e for e in reversed(conversation.exchanges) if e.answer and not e.error), None)

    def copy_answer(self) -> None:
        exchange = self._last_answer()
        if exchange is not None:
            QGuiApplication.clipboard().setText(exchange.answer)

    def save_to_notes(self) -> None:
        exchange = self._last_answer()
        if exchange is None or self.paper is None or self.library is None:
            return
        paper = self.library.get_paper(self.paper.id)
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        block = f"[{stamp} · Ask AI · {exchange.model}]\nQ: {exchange.question}\n\n{exchange.answer.strip()}\n"
        notes = f"{paper.notes.rstrip()}\n\n{block}" if paper.notes.strip() else block
        updated = self.library.update_flags(paper.id, notes=notes)
        self.paperChanged.emit(updated)

    def new_conversation(self) -> None:
        if self.paper is not None:
            self.conversations.pop(self.paper.id, None)
            self._render()
            self._update_state()

    def ask_in_browser(self, name: str, url: str) -> None:
        """Subscription route: copy the prompt, open the chat site, show the PDF so that it can be dragged in."""
        if self.paper is None or self.library is None:
            return
        QGuiApplication.clipboard().setText(web_prompt(self.paper, self.question.toPlainText()))
        self._launch(QUrl(url))
        pdf = self.library.pdf_path(self.paper)
        if pdf is not None and pdf.is_file():
            self._reveal(pdf)
            how = "drag the PDF (shown in its folder) into the chat, paste the question (Ctrl+V) and send"
        else:
            how = "paste the question (Ctrl+V) and send (this document has no PDF on this computer)"
        self._update_state(note=f"{name} opened in the browser: {how}.")


__all__ = ["AssistantPanel", "AssistantSettingsDialog", "Conversation", "Exchange"]
