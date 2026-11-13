"""Inbox watcher (SPEC Milestone 4): PDFs saved into ``<library_root>/inbox/`` are imported automatically.

Pure Python (no Qt): file-system events come from ``watchdog`` when it is installed, and a periodic
rescan covers missed events, network drives and synced folders (or replaces ``watchdog`` entirely).
A file is handed to ``on_ready`` only once it is *stable*: same size and modification time for
``settle_s`` seconds, readable (Windows keeps files that are still being written locked) and starting
with the ``%PDF`` signature, so half-downloaded files are never imported. ``on_ready`` runs in the
watcher thread; the GUI marshals it to its own thread with a Qt signal.

Files that turn out to be duplicates are moved to ``inbox/Duplicates/`` (never deleted).
"""

from __future__ import annotations

import logging
import os
import shutil
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

INBOX_DIRNAME = "inbox"
DUPLICATES_DIRNAME = "Duplicates"
_IGNORED_DIRS = frozenset({DUPLICATES_DIRNAME.casefold(), "failed"})
_PARTIAL_SUFFIXES = (".crdownload", ".part", ".partial", ".download", ".tmp")


@dataclass(slots=True)
class _Seen:
    size: int
    mtime: float
    since: float


def _signature(path: Path) -> tuple[int, float] | None:
    try:
        st = path.stat()
    except OSError:
        return None
    return st.st_size, st.st_mtime


def looks_like_pdf(path: Path) -> bool:
    """Readable now (not locked by the writer) and carrying the PDF signature in its first kilobyte."""
    try:
        with path.open("rb") as fh:
            return b"%PDF" in fh.read(1024)
    except OSError:
        return False


def move_aside(path: Path, inbox: Path, folder: str = DUPLICATES_DIRNAME) -> Path:
    """Move ``path`` into ``inbox/<folder>/`` under a name that does not exist yet; returns the new path."""
    target_dir = Path(inbox) / folder
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / path.name
    n = 2
    while target.exists():
        target = target_dir / f"{path.stem} ({n}){path.suffix}"
        n += 1
    shutil.move(str(path), str(target))
    return target


class InboxWatcher:
    def __init__(
        self,
        folder: Path,
        on_ready: Callable[[Path], None],
        *,
        settle_s: float = 2.0,
        poll_s: float = 1.0,
        rescan_s: float = 15.0,
        use_watchdog: bool = True,
        recursive: bool = True,
    ) -> None:
        self.folder = Path(folder)
        self.on_ready = on_ready
        self.settle_s = settle_s
        self.poll_s = poll_s
        self.rescan_s = rescan_s
        self.recursive = recursive
        self._use_watchdog = use_watchdog
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._pending: dict[Path, _Seen | None] = {}
        self._dispatched: dict[Path, tuple[int, float]] = {}
        self._thread: threading.Thread | None = None
        self._observer: object | None = None
        self.backend = "polling"

    # ------------------------------------------------------------------ lifecycle
    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.running:
            return
        self.folder.mkdir(parents=True, exist_ok=True)
        self._stop.clear()
        if self._use_watchdog:
            self._start_observer()
        self._thread = threading.Thread(target=self._run, name="bibmanager-inbox", daemon=True)
        self._thread.start()
        log.info("Watching inbox %s (%s)", self.folder, self.backend)

    def stop(self, timeout_s: float = 5.0) -> None:
        self._stop.set()
        self._wake.set()
        observer = self._observer
        self._observer = None
        if observer is not None:
            try:
                observer.stop()  # type: ignore[attr-defined]
                observer.join(timeout_s)  # type: ignore[attr-defined]
            except Exception:  # pragma: no cover - best effort
                log.exception("Could not stop the inbox observer")
        if self._thread is not None:
            self._thread.join(timeout_s)
            self._thread = None

    def _start_observer(self) -> None:
        try:
            from watchdog.events import FileSystemEvent, FileSystemEventHandler
            from watchdog.observers import Observer
        except ImportError:
            return
        watcher = self

        class _Handler(FileSystemEventHandler):
            def on_any_event(self, event: FileSystemEvent) -> None:
                if event.is_directory:
                    return
                for raw in (getattr(event, "dest_path", ""), event.src_path):
                    if raw:
                        watcher.notify(Path(os.fsdecode(raw)))

        try:
            observer = Observer()
            observer.schedule(_Handler(), str(self.folder), recursive=self.recursive)
            observer.daemon = True
            observer.start()
        except Exception as exc:  # e.g. unsupported network share: polling still works
            log.warning("watchdog unavailable for %s (%s); polling instead", self.folder, exc)
            return
        self._observer = observer
        self.backend = "watchdog"

    # ------------------------------------------------------------------ events
    def _accepts(self, path: Path) -> bool:
        if path.suffix.lower() != ".pdf" or path.name.startswith((".", "~$")):
            return False
        if path.name.lower().endswith(_PARTIAL_SUFFIXES):
            return False
        try:
            relative = path.relative_to(self.folder)
        except ValueError:
            return False
        parents = relative.parts[:-1]
        if parents and not self.recursive:
            return False
        return not any(p.casefold() in _IGNORED_DIRS for p in parents)

    def notify(self, path: Path) -> None:
        """A (possible) change of ``path``; safe from any thread."""
        if self._accepts(path):
            with self._lock:
                self._pending.setdefault(path, None)
            self._wake.set()

    def scan(self) -> None:
        """Queue every PDF currently in the inbox (start-up and periodic safety net)."""
        if not self.folder.is_dir():
            return
        pattern = self.folder.rglob("*") if self.recursive else self.folder.glob("*")
        for path in pattern:
            if path.is_file():
                self.notify(path)

    def forget(self, path: Path) -> None:
        """Allow ``path`` to be dispatched again (e.g. after a failed import was fixed)."""
        with self._lock:
            self._dispatched.pop(path, None)

    # ------------------------------------------------------------------ loop
    def _run(self) -> None:
        next_scan = 0.0
        while not self._stop.is_set():
            now = time.monotonic()
            if now >= next_scan:
                self.scan()
                next_scan = now + (self.rescan_s if self.backend == "watchdog" else self.poll_s * 2)
            self._process(now)
            self._wake.wait(self.poll_s)
            self._wake.clear()

    def _process(self, now: float) -> None:
        with self._lock:
            pending = list(self._pending.items())
        for path, seen in pending:
            sig = _signature(path)
            if sig is None:  # vanished (moved away, renamed)
                with self._lock:
                    self._pending.pop(path, None)
                    self._dispatched.pop(path, None)
                continue
            if self._dispatched.get(path) == sig:
                with self._lock:
                    self._pending.pop(path, None)
                continue
            size, mtime = sig
            if seen is None or (seen.size, seen.mtime) != sig or size == 0:
                with self._lock:
                    self._pending[path] = _Seen(size, mtime, now)
                continue
            if now - seen.since < self.settle_s or not looks_like_pdf(path):
                if now - seen.since > max(60.0, self.settle_s * 10) and not looks_like_pdf(path):
                    log.warning("Inbox: %s is not a readable PDF; ignored", path.name)
                    with self._lock:
                        self._pending.pop(path, None)
                        self._dispatched[path] = sig
                continue
            with self._lock:
                self._pending.pop(path, None)
                self._dispatched[path] = sig
            try:
                self.on_ready(path)
            except Exception:  # never kill the watcher thread
                log.exception("Inbox callback failed for %s", path)
