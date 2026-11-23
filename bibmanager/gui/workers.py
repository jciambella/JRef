"""Background execution (architecture invariant 4: PyMuPDF and HTTP never run on the GUI thread).

``TaskRunner`` runs a callable in a ``QThreadPool`` and delivers its result to a callback on
the GUI thread. Signals are always connected to *bound slots of a QObject living on the GUI
thread*, so Qt queues the delivery across threads (lambdas would run in the worker thread).
"""

from __future__ import annotations

import itertools
import logging
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import Any

from PySide6.QtCore import QCoreApplication, QObject, QRunnable, QThreadPool, Signal, Slot

from ..core.export import ZipPlan, write_zip
from ..core.inbox import move_aside
from ..core.library import AttachmentCandidate, IngestResult, IngestStatus, Library, ReferenceCandidate
from ..core.mendeley import (
    MendeleyImportOptions,
    MendeleyImportPlan,
    MendeleyImportReport,
    folder_name_for,
    plan_import,
)
from ..core.relink import RelinkResult, find_missing_pdfs

log = logging.getLogger(__name__)


class _Signals(QObject):
    done = Signal(int, object)
    error = Signal(int, str)


class _Task(QRunnable):
    def __init__(self, token: int, fn: Callable[..., Any], args: tuple[Any, ...], kwargs: dict[str, Any]) -> None:
        super().__init__()
        self.setAutoDelete(False)  # the Python side keeps ownership until delivery
        self.token = token
        self.fn, self.args, self.kwargs = fn, args, kwargs
        self.signals = _Signals()  # created on the GUI thread -> lives there

    def run(self) -> None:
        try:
            result = self.fn(*self.args, **self.kwargs)
        except Exception as exc:  # delivered to the GUI as a message, never raised in the pool
            log.exception("Background task failed")
            self.signals.error.emit(self.token, f"{type(exc).__name__}: {exc}")
        else:
            self.signals.done.emit(self.token, result)


class TaskRunner(QObject):
    def __init__(self, pool: QThreadPool, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.pool = pool
        self._tokens = itertools.count(1)
        self._tasks: dict[int, tuple[_Task, Callable[[Any], None], Callable[[str], None] | None]] = {}

    @property
    def pending(self) -> int:
        return len(self._tasks)

    def submit(
        self,
        fn: Callable[..., Any],
        *args: Any,
        on_done: Callable[[Any], None],
        on_error: Callable[[str], None] | None = None,
        **kwargs: Any,
    ) -> int:
        token = next(self._tokens)
        task = _Task(token, fn, args, kwargs)
        task.signals.done.connect(self._deliver)
        task.signals.error.connect(self._deliver_error)
        self._tasks[token] = (task, on_done, on_error)
        self.pool.start(task)
        return token

    @Slot(int, object)
    def _deliver(self, token: int, result: object) -> None:
        entry = self._tasks.pop(token, None)
        if entry is not None:
            entry[1](result)

    @Slot(int, str)
    def _deliver_error(self, token: int, message: str) -> None:
        entry = self._tasks.pop(token, None)
        if entry is not None and entry[2] is not None:
            entry[2](message)

    def wait_idle(self, timeout_s: float = 30.0) -> None:
        """Process events until every submitted task has been delivered (tests, shutdown)."""
        deadline = time.monotonic() + timeout_s
        while self._tasks:
            self.pool.waitForDone(20)
            QCoreApplication.processEvents()
            if time.monotonic() > deadline:
                raise TimeoutError(f"{len(self._tasks)} background task(s) still running")


class ImportController(QObject):
    """Drag & drop / "Add Files" pipeline: ``prepare_import`` in the pool, ``commit_import`` on the GUI thread."""

    progress = Signal(int, int)  # done, total
    imported = Signal(object)  # IngestResult
    failed = Signal(str, str)  # path, message
    batchFinished = Signal(int, int, int)  # new, duplicates, failures

    def __init__(self, library: Library, runner: TaskRunner, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.library = library
        self.runner = runner
        _ = library.resolver  # create the shared HTTP client on the GUI thread
        self._pending = 0
        self._total = self._done = self._new = self._dups = self._failures = 0

    @property
    def pending(self) -> int:
        return self._pending

    def import_files(
        self,
        paths: Iterable[Path],
        collection_id: int | None = None,
        *,
        mode: str | None = None,
        inbox: Path | None = None,
    ) -> int:
        """``mode`` overrides the configured move/copy; with ``inbox`` set, files are always moved and
        duplicates are set aside in ``inbox/Duplicates/`` instead of staying in the inbox."""
        paths = list(paths)
        if not paths:
            return 0
        if self._pending == 0:
            self._total = self._done = self._new = self._dups = self._failures = 0
        self._total += len(paths)
        for path in paths:
            self._pending += 1
            self.runner.submit(
                self.library.prepare_import,
                path,
                check_duplicate=True,
                on_done=partial(self._commit, collection_id=collection_id, mode=mode, inbox=inbox),
                on_error=partial(self._fail, str(path)),
            )
        self.progress.emit(self._done, self._total)
        return len(paths)

    def _commit(self, candidate: Any, collection_id: int | None, mode: str | None, inbox: Path | None) -> None:
        try:
            result: IngestResult = self.library.commit_import(candidate, collection_id, mode=mode)
            if inbox is not None and result.status is not IngestStatus.IMPORTED and candidate.source_path.exists():
                moved = move_aside(candidate.source_path, inbox)
                result.log.append(f"Duplicate moved to {moved}")
        except Exception as exc:
            log.exception("Import failed for %s", candidate.source_path)
            self._fail(str(candidate.source_path), f"{type(exc).__name__}: {exc}")
            return
        if result.status is IngestStatus.IMPORTED:
            self._new += 1
        else:
            self._dups += 1
        self.imported.emit(result)
        self._step()

    def _fail(self, path: str, message: str) -> None:
        self._failures += 1
        self.failed.emit(path, message)
        self._step()

    def _step(self) -> None:
        self._pending -= 1
        self._done += 1
        self.progress.emit(self._done, self._total)
        if self._pending == 0:
            self.batchFinished.emit(self._new, self._dups, self._failures)


class MendeleyImportController(QObject):
    """Mendeley/.bib import: ``plan_import`` + ``prepare_reference`` (hash, full text) in the pool,
    ``commit_reference`` on the GUI thread inside one ``Library.bulk_update`` (every .bib written once)."""

    planned = Signal(object)  # MendeleyImportPlan
    progress = Signal(int, int)
    finished = Signal(object)  # MendeleyImportReport

    def __init__(self, library: Library, runner: TaskRunner, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.library = library
        self.runner = runner
        self.options: MendeleyImportOptions | None = None
        self.report = MendeleyImportReport()
        self._pending = 0
        self._done = 0
        self._running = False
        self._folder_ids: dict[tuple[str, ...], int] = {}

    @property
    def running(self) -> bool:
        return self._running

    def start(self, options: MendeleyImportOptions) -> None:
        if self._running:
            raise RuntimeError("A Mendeley import is already running")
        self.options = options
        self.report = MendeleyImportReport()
        self._folder_ids = {}
        self._running = True
        self.runner.submit(plan_import, options, on_done=self._on_planned, on_error=self._on_plan_failed)

    def cancel(self) -> None:
        if self._running:
            self.report.cancelled = True

    def _on_plan_failed(self, message: str) -> None:
        self.report.warnings.append(message)
        self._finish()

    def _on_planned(self, plan: MendeleyImportPlan) -> None:
        assert self.options is not None
        self.planned.emit(plan)
        self.report.total = len(plan.candidates)
        self.report.warnings += plan.warnings
        self.report.database_matches = plan.database_matches
        if self.report.cancelled or not plan.candidates:
            self._finish()
            return
        self.library.begin_bulk()
        self._pending = len(plan.candidates)
        self._done = 0
        self.progress.emit(0, self._pending)
        for cand in plan.candidates:
            self.runner.submit(
                self.library.prepare_reference,
                cand,
                index_fulltext=self.options.index_fulltext,
                on_done=self._commit,
                on_error=partial(self._failed, cand.key),
            )

    def _folder_id(self, path: tuple[str, ...]) -> int:
        if path not in self._folder_ids:
            self._folder_ids[path] = self.library.ensure_collection_path(path)
        return self._folder_ids[path]

    def _commit(self, cand: ReferenceCandidate) -> None:
        assert self.options is not None
        if not self.report.cancelled:
            try:
                folder_ids = [self._folder_id(path) for path in cand.folders if any(path)]
                result = self.library.commit_reference(
                    cand, file_mode=self.options.file_mode, keep_key=self.options.keep_keys, folder_ids=folder_ids
                )
            except Exception as exc:
                log.exception("Mendeley import failed for %s", cand.key)
                self.report.failed.append(f"{cand.key}: {type(exc).__name__}: {exc}")
            else:
                if result.status is IngestStatus.IMPORTED:
                    self.report.imported += 1
                    if not result.paper.has_pdf:
                        self.report.without_pdf += 1
                    elif result.paper.pdf_linked:
                        self.report.linked += 1
                    else:
                        self.report.stored += 1
                else:
                    self.report.duplicates += 1
                    self.report.attached_to_existing += int(result.attached_pdf)
                self.report.supplements += result.supplements
                self.report.missing_pdfs += [f"{cand.key}: {path}" for path in cand.missing_files]
        self._step()

    def _failed(self, key: str, message: str) -> None:
        self.report.failed.append(f"{key}: {message}")
        self._step()

    def _step(self) -> None:
        self._done += 1
        self._pending -= 1
        self.progress.emit(self._done, self._done + self._pending)
        if self._pending == 0:
            self._finish()

    def _finish(self) -> None:
        options = self.options
        try:
            if options is not None and options.take_over_bib_files and not self.report.cancelled:
                for source in options.bib_paths:
                    name = folder_name_for(options, Path(source))
                    cid = self._folder_ids.get((name,)) if name else None
                    if cid is not None:
                        self.library.set_collection_export_path(cid, Path(source), overwrite_foreign=True)
        except Exception as exc:  # the import itself succeeded; report the export problem
            self.report.warnings.append(f"Could not take over the .bib files: {exc}")
        finally:
            self.report.folders = len(self._folder_ids)
            self.library.end_bulk()  # no-op if begin_bulk() was never called
            self._running = False
            self.finished.emit(self.report)


@dataclass(slots=True)
class AttachReport:
    attached: list[str] = field(default_factory=list)  # citation keys
    failed: list[str] = field(default_factory=list)  # "key: reason"
    searched: int = 0  # entries without a PDF (Find Missing PDFs)
    scanned: int = 0  # PDFs found in the folder (Find Missing PDFs)
    ambiguous: list[str] = field(default_factory=list)  # "key: file1 | file2"
    rules: dict[str, int] = field(default_factory=dict)  # matching rule -> count


class AttachController(QObject):
    """Attach PDFs to existing entries: hashing/text extraction in the pool, ``Library.attach_pdf`` on the GUI
    thread. Also runs "Find Missing PDFs" (``core.relink``) for many entries at once."""

    progress = Signal(int, int)
    attached = Signal(object)  # Paper
    finished = Signal(object)  # AttachReport

    def __init__(self, library: Library, runner: TaskRunner, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.library = library
        self.runner = runner
        self.report = AttachReport()
        self._pending = 0
        self._total = 0
        self._running = False
        self._bulk = False

    @property
    def running(self) -> bool:
        return self._running

    def attach(self, items: Iterable[tuple[int, Path]], mode: str | None = None) -> int:
        """Attach ``(paper_id, pdf_path)`` pairs (``mode``: link / copy / move; default: Preferences)."""
        items = list(items)
        if self._running:
            raise RuntimeError("An attach operation is already running")
        self.report = AttachReport()
        self._start(items, mode)
        return len(items)

    def find_missing(self, folders: Iterable[Path], mode: str) -> None:
        if self._running:
            raise RuntimeError("An attach operation is already running")
        self.report = AttachReport()
        self._running = True
        papers = self.library.papers_without_pdf()  # snapshot taken on the GUI thread
        exclude = [p for p in (self.library.pdf_path(x) for x in self.library.papers() if x.has_pdf) if p]
        self.runner.submit(
            find_missing_pdfs,
            papers,
            [Path(f) for f in folders],
            exclude=exclude,
            on_done=partial(self._on_matched, mode=mode),
            on_error=self._on_match_failed,
        )

    def _on_match_failed(self, message: str) -> None:
        self.report.failed.append(message)
        self._running = False
        self.finished.emit(self.report)

    def _on_matched(self, result: RelinkResult, mode: str) -> None:
        self.report.searched, self.report.scanned = result.searched, result.scanned
        for paper_id, paths in result.ambiguous:
            key = self._key(paper_id)
            self.report.ambiguous.append(f"{key}: " + " | ".join(str(p) for p in paths))
        for match in result.matches:
            self.report.rules[match.rule] = self.report.rules.get(match.rule, 0) + 1
        self._running = False
        self._start([(m.paper_id, m.path) for m in result.matches], mode)

    def _key(self, paper_id: int) -> str:
        try:
            return self.library.get_paper(paper_id).citation_key
        except KeyError:
            return f"#{paper_id}"

    def _start(self, items: list[tuple[int, Path]], mode: str | None) -> None:
        self._running = True
        self._pending = self._total = len(items)
        if not items:
            self._finish()
            return
        self._bulk = len(items) > 1
        if self._bulk:
            self.library.begin_bulk()
        self.progress.emit(0, self._total)
        for paper_id, path in items:
            self.runner.submit(
                self.library.prepare_attachment,
                path,
                on_done=partial(self._commit, paper_id=paper_id, mode=mode),
                on_error=partial(self._failed, paper_id),
            )

    def _commit(self, candidate: AttachmentCandidate, paper_id: int, mode: str | None) -> None:
        try:
            paper = self.library.attach_pdf(paper_id, candidate, mode=mode)
        except Exception as exc:
            log.warning("Cannot attach %s: %s", candidate.path, exc)
            self.report.failed.append(f"{self._key(paper_id)}: {exc}")
        else:
            self.report.attached.append(paper.citation_key)
            self.attached.emit(paper)
        self._step()

    def _failed(self, paper_id: int, message: str) -> None:
        self.report.failed.append(f"{self._key(paper_id)}: {message}")
        self._step()

    def _step(self) -> None:
        self._pending -= 1
        self.progress.emit(self._total - self._pending, self._total)
        if self._pending == 0:
            self._finish()

    def _finish(self) -> None:
        if self._bulk:
            self.library.end_bulk()
            self._bulk = False
        self._running = False
        self.finished.emit(self.report)


def expand_pdf_paths(paths: Iterable[str | Path]) -> list[Path]:
    """Files are kept if they are PDFs; folders are searched recursively for PDFs."""
    found: list[Path] = []
    for raw in paths:
        path = Path(raw)
        if path.is_dir():
            found += sorted(p for p in path.rglob("*") if p.is_file() and p.suffix.lower() == ".pdf")
        elif path.is_file() and path.suffix.lower() == ".pdf":
            found.append(path)
    return list(dict.fromkeys(found))


class ZipExportController(QObject):
    """Right-click on a project > Export PDFs as ZIP: the plan is made on the GUI thread
    (``Library.plan_project_zip``), the archive is written in the pool (``core.export.write_zip``, file I/O only).

    ``progress`` is emitted from the worker thread and reaches bound slots on the GUI thread through Qt's queued
    connection; it carries Python ints because an archive can be larger than 2 GB.
    """

    progress = Signal(object, object)  # bytes done, bytes in total
    finished = Signal(object)  # ZipReport (``cancelled`` set if the user cancelled)
    failed = Signal(str)

    PROGRESS_INTERVAL_S = 0.1

    def __init__(self, runner: TaskRunner, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.runner = runner
        self._cancel: threading.Event | None = None
        self._last_report = 0.0

    @property
    def running(self) -> bool:
        return self._cancel is not None

    def start(self, plan: ZipPlan, target: Path) -> None:
        if self.running:
            raise RuntimeError("A ZIP export is already running")
        self._cancel = threading.Event()
        self._last_report = 0.0
        self.runner.submit(
            write_zip,
            plan,
            target,
            progress=self._report_progress,
            cancel=self._cancel,
            on_done=self._on_done,
            on_error=self._on_failed,
        )

    def cancel(self) -> None:
        if self._cancel is not None:
            self._cancel.set()

    def _report_progress(self, done: int, total: int) -> None:  # worker thread
        now = time.monotonic()
        if done < total and now - self._last_report < self.PROGRESS_INTERVAL_S:
            return
        self._last_report = now
        self.progress.emit(done, total)

    def _on_done(self, report: Any) -> None:
        self._cancel = None
        self.finished.emit(report)

    def _on_failed(self, message: str) -> None:
        self._cancel = None
        self.failed.emit(message)
