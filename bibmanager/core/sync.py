"""Sharing one library between devices through a synced folder (Dropbox, Syncthing, Qsync, Drive, ...).

Why not simply put ``library.db`` in the synced folder: a SQLite file written from two devices ends as
"conflicted copy" files (or a corrupt database when the sync client uploads it in the middle of a transaction),
and every small change re-uploads the whole database. Instead:

* every device keeps its **own** database outside the synced folder (``<data_dir>/libraries/<id>/library.db``);
* the synced folder holds the PDFs, the ``.bib`` files and ``sync/<device>/NNNNNN.jsonl``: an append-only
  **journal** of the changes made on that device. A device only ever writes its own journal, so the sync client
  never sees two versions of one file;
* ``SyncEngine.push`` compares the database with the state last journaled (``sync_shadow``) and appends the
  differences; ``SyncEngine.pull`` replays the other devices' journals.

Merging is deterministic, so all devices converge whatever the order in which the files arrive:

* every row has a ``uid``; every change carries a hybrid logical clock stamp (wall-clock ms, counter, device).
  Each field is a last-writer-wins register; the PDF identity (key, path, linked flag, hash) is one register;
* deleting wins over editing; a folder membership is a last-writer-wins flag per (paper, folder);
* two entries that got the same citation key on two devices: the one with the larger ``uid`` gets the next
  suffix (``Smith_2020a``), on every device alike. This includes the same paper added on two devices before
  they synchronised: both entries are kept and reported (``SyncReport.duplicates``) -- nothing is ever merged
  or deleted automatically, because no rule for that is safe when changes arrive in any order;
* PDF files are carried by the sync client itself. Applying a remote change never moves or deletes a file: an
  entry whose PDF has not arrived yet simply has no file for a while.

This module is pure Python and uses only the ``Library``/``Database`` API plus SQL on the ``sync_*`` tables.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import socket
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .. import APP_NAME, __version__
from .models import PaperMetadata, authors_from_json

if TYPE_CHECKING:  # pragma: no cover
    from .config import AppConfig
    from .library import Library

log = logging.getLogger(__name__)

SYNC_DIRNAME = "sync"
LIBRARY_FILE = "library.json"
DEVICE_FILE = "device.json"
JOURNAL_FORMAT = 1
SEGMENT_BYTES = 256_000  # a journal file is closed at about this size (small files sync quickly)
DB_FILENAME = "library.db"

# Synchronised columns of ``papers``. ``id`` is local; the full text index is rebuilt per device.
FILE_GROUP = ("citation_key", "relative_pdf_path", "pdf_linked", "sha256")
PAPER_FIELDS = (
    "doi", "arxiv_id", "entry_type", "title", "authors_json", "year", "journal", "volume", "issue", "pages",
    "publisher", "abstract", "url", "isbn", "issn", "pmid", "keywords", "extra_fields", "needs_review", "notes",
    "tags", "favorite", "is_read", "last_opened_at", "my_publication", "created_at", *FILE_GROUP,
)  # fmt: skip
_META_COLUMNS = frozenset({"title", "authors_json", "abstract"})  # what the full-text index mirrors
COLLECTION_FIELDS = ("name", "slug", "parent", "created_at")  # bib_export_path is a path on one device: local
# Supplementary files: the file identity (path, linked flag, hash, position) is one register, like a paper's PDF.
ATTACHMENT_FILE_GROUP = ("relative_path", "linked", "sha256", "position")
ATTACHMENT_FIELDS = ("paper", "label", "created_at", *ATTACHMENT_FILE_GROUP)
_FILE = "@file"
_SEGMENT = re.compile(r"^\d{6}\.jsonl$")
_DEVICE_ID = re.compile(r"^[0-9a-f]{8,32}$")


class SyncError(RuntimeError):
    pass


@dataclass(slots=True)
class SyncReport:
    pushed: int = 0  # changes of this device written to its journal
    applied: int = 0  # changes of other devices applied here
    papers_added: int = 0
    papers_updated: int = 0
    papers_deleted: int = 0
    duplicates: list[tuple[str, str]] = field(default_factory=list)  # (key, key) of the same PDF/DOI added twice
    waiting: int = 0  # changes that refer to something not received yet
    devices: int = 0  # other devices seen in the folder
    messages: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.applied)

    def summary(self) -> str:
        if not self.pushed and not self.applied:
            return "Up to date"
        parts = []
        if self.pushed:
            parts.append(f"{self.pushed} sent")
        if self.applied:
            parts.append(f"{self.applied} received")
        if self.papers_added:
            parts.append(f"{self.papers_added} new")
        if self.duplicates:
            parts.append(f"{len(self.duplicates)} possible duplicates")
        return ", ".join(parts)


@dataclass(slots=True)
class DeviceInfo:
    device_id: str
    name: str
    app: str = ""
    is_self: bool = False
    last_change: float = 0.0  # mtime of the newest journal file (seconds since the epoch), 0 if none


# --------------------------------------------------------------------------- locations and identity
def sync_dir(root: Path) -> Path:
    return Path(root) / SYNC_DIRNAME


def is_shared(root: Path) -> bool:
    """True once a library folder has been set up for sharing (by any device)."""
    return (sync_dir(root) / LIBRARY_FILE).is_file()


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex[:8]}.tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def library_id(root: Path, *, create: bool = True) -> str:
    path = sync_dir(root) / LIBRARY_FILE
    found = str(_read_json(path).get("id") or "")
    if found or not create:
        return found
    found = uuid.uuid4().hex
    _write_json(path, {"id": found, "format": JOURNAL_FORMAT, "app": APP_NAME})
    return found


def device_identity(data_dir: Path, name: str = "") -> tuple[str, str]:
    """``(id, name)`` of this device; the id is created once and kept in the per-device data folder."""
    path = Path(data_dir) / DEVICE_FILE
    data = _read_json(path)
    device_id = str(data.get("id") or "")
    if not _DEVICE_ID.match(device_id):
        device_id = uuid.uuid4().hex[:12]
    label = name or str(data.get("name") or "") or socket.gethostname() or "device"
    if data.get("id") != device_id or data.get("name") != label:
        _write_json(path, {"id": device_id, "name": label})
    return device_id, label


def local_database_path(root: Path, data_dir: Path) -> Path:
    return Path(data_dir) / "libraries" / library_id(root) / DB_FILENAME


def prepare_database(root: Path, config: AppConfig) -> Path:
    """Path of this device's database for a shared library.

    The first time a library is shared, the database that lived in the library folder is *moved* to the
    per-device data folder (ids, full-text index and settings are kept); the folder then only contains files
    that are safe to synchronise.
    """
    root = Path(root)
    target = local_database_path(root, config.data_dir)
    in_folder = root / DB_FILENAME
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        if in_folder.is_file() and config.adopt_folder_database:
            log.info("Moving %s to %s (the library is now shared between devices)", in_folder, target)
            _checkpoint(in_folder)
            shutil.move(str(in_folder), str(target))
            for suffix in ("-wal", "-shm", "-journal"):
                Path(str(in_folder) + suffix).unlink(missing_ok=True)
    return target


def _checkpoint(path: Path) -> None:
    try:
        conn = sqlite3.connect(str(path))
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            conn.close()
    except sqlite3.Error:  # pragma: no cover - best effort
        log.warning("Could not checkpoint %s", path)


# --------------------------------------------------------------------------- the engine
class SyncEngine:
    """Journal writer/reader of one device. Use from the thread that owns the ``Library``."""

    def __init__(self, library: Library) -> None:
        self.library = library
        self.db = library.db
        self.conn = library.db.conn
        self.dir = sync_dir(library.root)
        self.library_id = library_id(library.root)
        self.device_id, self.device_name = device_identity(library.config.data_dir, library.config.device_name)
        self.own_dir = self.dir / self.device_id
        self.own_dir.mkdir(parents=True, exist_ok=True)
        info = {"id": self.device_id, "name": self.device_name, "app": f"{APP_NAME} {__version__}"}
        if _read_json(self.own_dir / DEVICE_FILE) != info:
            _write_json(self.own_dir / DEVICE_FILE, info)
        self._clock = self._load_clock()
        self._changes_seen = -1
        self._sizes_seen: dict[str, int] = {}
        self.last_report = SyncReport()
        self.last_sync = 0.0

    # ------------------------------------------------------------------ small helpers
    def _meta_get(self, key: str, default: str = "") -> str:
        row = self.conn.execute("SELECT value FROM sync_meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row and row[0] is not None else default

    def _meta_set(self, key: str, value: str) -> None:
        self.conn.execute("INSERT OR REPLACE INTO sync_meta (key, value) VALUES (?, ?)", (key, value))

    def _load_clock(self) -> tuple[int, int]:
        ms, _, counter = self._meta_get("hlc", "0.0").partition(".")
        return int(ms or 0), int(counter or 0)

    def _stamp(self) -> str:
        """Next hybrid-logical-clock stamp: never decreases, sorts as text, unique per device."""
        now = int(time.time() * 1000)
        ms, counter = self._clock
        self._clock = (now, 0) if now > ms else (ms, counter + 1)
        return f"{self._clock[0]:013d}.{self._clock[1]:05d}.{self.device_id}"

    def _observe(self, stamp: str) -> None:
        try:
            ms, counter = int(stamp[:13]), int(stamp[14:19])
        except ValueError:
            return
        if (ms, counter) > self._clock:
            self._clock = (ms, counter)

    def _ts(self, *keys: str) -> str:
        """Stamp of the first key that has one (specific register, then the row's creation)."""
        for key in keys:
            row = self.conn.execute("SELECT ts FROM sync_clock WHERE key = ?", (key,)).fetchone()
            if row:
                return row[0]
        return ""

    def _set_ts(self, key: str, stamp: str) -> None:
        self.conn.execute("INSERT OR REPLACE INTO sync_clock (key, ts) VALUES (?, ?)", (key, stamp))

    def _shadow(self, kind: str, uid: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT data FROM sync_shadow WHERE kind = ? AND uid = ?", (kind, uid)).fetchone()
        return json.loads(row[0]) if row else None

    def _set_shadow(self, kind: str, uid: str, data: dict[str, Any] | None) -> None:
        if data is None:
            self.conn.execute("DELETE FROM sync_shadow WHERE kind = ? AND uid = ?", (kind, uid))
        else:
            self.conn.execute(
                "INSERT OR REPLACE INTO sync_shadow (kind, uid, data) VALUES (?, ?, ?)", (kind, uid, _dumps(data))
            )

    @staticmethod
    def _register(name: str) -> str:
        return _FILE if name in FILE_GROUP else name

    @staticmethod
    def _attachment_register(name: str) -> str:
        return _FILE if name in ATTACHMENT_FILE_GROUP else name

    # ------------------------------------------------------------------ current state
    def _paper_states(self) -> dict[str, dict[str, Any]]:
        self.conn.execute("UPDATE papers SET uid = lower(hex(randomblob(16))) WHERE uid IS NULL")
        columns = ", ".join(PAPER_FIELDS)
        return {
            row["uid"]: {name: row[name] for name in PAPER_FIELDS}
            for row in self.conn.execute(f"SELECT uid, {columns} FROM papers")
        }

    def _collection_states(self) -> dict[str, dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT c.uid, c.name, c.slug, c.created_at, p.uid AS parent FROM collections c "
            "LEFT JOIN collections p ON p.id = c.parent_id ORDER BY c.id"
        )
        return {row["uid"]: {name: row[name] for name in COLLECTION_FIELDS} for row in rows}

    def _attachment_states(self) -> dict[str, dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT a.uid, p.uid AS paper, a.relative_path, a.linked, a.sha256, a.label, a.position, a.created_at "
            "FROM attachments a JOIN papers p ON p.id = a.paper_id ORDER BY a.id"
        )
        return {row["uid"]: {name: row[name] for name in ATTACHMENT_FIELDS} for row in rows}

    def _member_pairs(self) -> set[str]:
        rows = self.conn.execute(
            "SELECT p.uid, c.uid FROM paper_collections pc JOIN papers p ON p.id = pc.paper_id "
            "JOIN collections c ON c.id = pc.collection_id"
        )
        return {f"{p}|{c}" for p, c in rows}

    # ------------------------------------------------------------------ push
    def push(self, *, force: bool = False) -> int:
        """Append this device's changes since the last push to its journal. Returns the number of changes."""
        if not force and self.conn.total_changes == self._changes_seen:
            return 0
        ops: list[dict[str, Any]] = []
        after: list[tuple[str, str, dict[str, Any] | None]] = []  # shadow updates (kind, uid, data)
        clocks: list[tuple[str, int]] = []  # (clock key, index into ops)

        def emit(op: dict[str, Any], *keys: str) -> None:
            ops.append(op)
            clocks.extend((key, len(ops) - 1) for key in keys)

        with self.db.transaction():
            shadows: dict[str, dict[str, str]] = {"c": {}, "p": {}, "m": {}, "a": {}}
            for kind, uid, data in self.conn.execute("SELECT kind, uid, data FROM sync_shadow"):
                shadows.setdefault(kind, {})[uid] = data

            collections = self._collection_states()
            for uid, state in collections.items():  # ordered by id: parents come first
                old_text = shadows["c"].get(uid)
                if old_text is None:
                    emit({"k": "c", "u": uid, "new": 1, "f": state}, f"c:{uid}")
                elif old_text != _dumps(state):
                    old = json.loads(old_text)
                    changed = {name: value for name, value in state.items() if old.get(name) != value}
                    emit({"k": "c", "u": uid, "f": changed}, *(f"c:{uid}:{name}" for name in changed))
                else:
                    continue
                after.append(("c", uid, state))

            papers = self._paper_states()
            for uid, state in papers.items():
                old_text = shadows["p"].get(uid)
                if old_text is None:
                    emit({"k": "p", "u": uid, "new": 1, "f": state}, f"p:{uid}")
                elif old_text != _dumps(state):
                    old = json.loads(old_text)
                    changed = {name: value for name, value in state.items() if old.get(name) != value}
                    if any(name in FILE_GROUP for name in changed):  # the PDF identity travels as a whole
                        changed.update({name: state[name] for name in FILE_GROUP})
                    emit(
                        {"k": "p", "u": uid, "f": changed},
                        *{f"p:{uid}:{self._register(name)}" for name in changed},
                    )
                else:
                    continue
                after.append(("p", uid, state))

            pairs = self._member_pairs()
            for pair in sorted(pairs - shadows["m"].keys()):
                paper_uid, collection_uid = pair.split("|")
                emit({"k": "m", "p": paper_uid, "c": collection_uid, "on": 1}, f"m:{pair}")
                after.append(("m", pair, {}))
            for pair in sorted(shadows["m"].keys() - pairs):
                paper_uid, collection_uid = pair.split("|")
                if paper_uid in papers and collection_uid in collections:  # else implied by the deletion
                    emit({"k": "m", "p": paper_uid, "c": collection_uid, "on": 0}, f"m:{pair}")
                after.append(("m", pair, None))

            attachments = self._attachment_states()
            for uid, state in attachments.items():
                old_text = shadows["a"].get(uid)
                if old_text is None:
                    emit({"k": "a", "u": uid, "new": 1, "f": state}, f"a:{uid}")
                elif old_text != _dumps(state):
                    old = json.loads(old_text)
                    changed = {name: value for name, value in state.items() if old.get(name) != value}
                    if any(name in ATTACHMENT_FILE_GROUP for name in changed):
                        changed.update({name: state[name] for name in ATTACHMENT_FILE_GROUP})
                    emit({"k": "a", "u": uid, "f": changed}, *{f"a:{uid}:{self._attachment_register(n)}" for n in changed})
                else:
                    continue
                after.append(("a", uid, state))
            for uid in shadows["a"].keys() - attachments.keys():
                paper_uid = json.loads(shadows["a"][uid]).get("paper")
                if paper_uid in papers:  # else implied by the deletion of the paper
                    emit({"k": "a", "u": uid, "del": 1}, f"ad:{uid}")
                after.append(("a", uid, None))

            for uid in shadows["p"].keys() - papers.keys():
                emit({"k": "p", "u": uid, "del": 1}, f"pd:{uid}")
                after.append(("p", uid, None))
            for uid in shadows["c"].keys() - collections.keys():
                emit({"k": "c", "u": uid, "del": 1}, f"cd:{uid}")
                after.append(("c", uid, None))

            if ops:
                for op in ops:
                    op["t"] = self._stamp()
                self._append(ops)  # journal first: a crash before the commit only repeats these changes
                for key, index in clocks:
                    self._set_ts(key, ops[index]["t"])
                for kind, uid, data in after:
                    self._set_shadow(kind, uid, data)
                self._meta_set("hlc", f"{self._clock[0]}.{self._clock[1]}")
        self._changes_seen = self.conn.total_changes
        return len(ops)

    def _segments(self, directory: Path) -> list[Path]:
        try:
            return sorted(p for p in directory.iterdir() if _SEGMENT.match(p.name))
        except OSError:
            return []

    def _append(self, ops: list[dict[str, Any]]) -> None:
        segments = self._segments(self.own_dir)
        target = segments[-1] if segments else self.own_dir / "000001.jsonl"
        if segments and target.stat().st_size >= SEGMENT_BYTES:
            target = self.own_dir / f"{int(target.stem) + 1:06d}.jsonl"
        payload = "".join(_dumps(op) + "\n" for op in ops).encode("utf-8")
        with open(target, "ab") as fh:
            fh.write(payload)
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except OSError:  # pragma: no cover - e.g. FUSE mounts that refuse fsync
                pass
        # Own changes are already in the database: move the reading position past them.
        self.conn.execute(
            "INSERT OR REPLACE INTO sync_cursor (device, file, offset) VALUES (?, ?, ?)",
            (self.device_id, target.name, target.stat().st_size),
        )

    # ------------------------------------------------------------------ pull
    def _device_dirs(self) -> list[Path]:
        try:
            return sorted(p for p in self.dir.iterdir() if p.is_dir() and _DEVICE_ID.match(p.name))
        except OSError:
            return []

    def has_incoming(self) -> bool:
        """Cheap check (file sizes only) whether any journal grew since the last pull."""
        sizes = {}
        for directory in self._device_dirs():
            for segment in self._segments(directory):
                try:
                    sizes[f"{directory.name}/{segment.name}"] = segment.stat().st_size
                except OSError:
                    continue
        changed = sizes != self._sizes_seen
        self._sizes_seen = sizes
        return changed

    def _read_new_ops(self) -> tuple[list[dict[str, Any]], list[tuple[str, str, int]]]:
        cursors = {(d, f): o for d, f, o in self.conn.execute("SELECT device, file, offset FROM sync_cursor")}
        ops: list[dict[str, Any]] = []
        moved: list[tuple[str, str, int]] = []
        for directory in self._device_dirs():
            for segment in self._segments(directory):
                offset = int(cursors.get((directory.name, segment.name), 0))
                try:
                    if segment.stat().st_size <= offset:
                        continue
                    with open(segment, "rb") as fh:
                        fh.seek(offset)
                        data = fh.read()
                except OSError as exc:
                    log.warning("Cannot read journal %s: %s", segment, exc)
                    continue
                end = data.rfind(b"\n") + 1  # a line still being transferred is left for the next time
                if end <= 0:
                    continue
                for line in data[:end].splitlines():
                    if not line.strip():
                        continue
                    try:
                        op = json.loads(line)
                    except ValueError:
                        log.warning("Skipping an unreadable line in %s", segment)
                        continue
                    if isinstance(op, dict) and isinstance(op.get("t"), str) and op.get("k") in ("p", "c", "m", "a"):
                        ops.append(op)
                moved.append((directory.name, segment.name, offset + end))
        ops.sort(key=lambda op: op["t"])
        return ops, moved

    def pull(self) -> SyncReport:
        """Apply the changes other devices journaled since the last pull."""
        report = SyncReport(devices=max(0, len(self._device_dirs()) - 1))
        ops, moved = self._read_new_ops()
        self.has_incoming()  # remember the sizes just read
        if not moved:
            return report
        with self.db.transaction():
            for op in ops:
                self._observe(op["t"])
                self._apply(op, report)
            for device, name, offset in moved:
                self.conn.execute(
                    "INSERT OR REPLACE INTO sync_cursor (device, file, offset) VALUES (?, ?, ?)",
                    (device, name, offset),
                )
            self._meta_set("hlc", f"{self._clock[0]}.{self._clock[1]}")
            report.waiting = int(self.conn.execute("SELECT COUNT(*) FROM sync_pending").fetchone()[0])
        return report

    def sync(self) -> SyncReport:
        """Push, pull, and push again (what merging decided locally, e.g. a suffix after a key collision)."""
        pushed = self.push()
        report = self.pull()
        report.pushed = pushed + (self.push() if report.applied else 0)
        self.last_report, self.last_sync = report, time.time()
        return report

    # ------------------------------------------------------------------ applying one change
    def _apply(self, op: dict[str, Any], report: SyncReport) -> None:
        try:
            kind = op["k"]
            if kind == "p":
                done = self._apply_paper(op, report)
            elif kind == "c":
                done = self._apply_collection(op, report)
            elif kind == "a":
                done = self._apply_attachment(op)
            else:
                done = self._apply_member(op)
        except (KeyError, TypeError, ValueError, sqlite3.Error) as exc:
            log.warning("Skipping a change that could not be applied (%s): %s", exc, _dumps(op)[:200])
            report.messages.append(f"A change from another device could not be applied: {exc}")
            return
        if done:
            report.applied += 1

    def _wait(self, uid: str, op: dict[str, Any]) -> bool:
        self.conn.execute("INSERT INTO sync_pending (wait_uid, op) VALUES (?, ?)", (uid, _dumps(op)))
        return False

    def _release(self, uid: str, report: SyncReport) -> None:
        rows = self.conn.execute("SELECT id, op FROM sync_pending WHERE wait_uid = ? ORDER BY id", (uid,)).fetchall()
        if not rows:
            return
        self.conn.execute("DELETE FROM sync_pending WHERE wait_uid = ?", (uid,))
        for op in sorted((json.loads(text) for _, text in rows), key=lambda o: o["t"]):
            self._apply(op, report)

    # -- papers
    def _paper_row(self, uid: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM papers WHERE uid = ?", (uid,)).fetchone()

    def _apply_paper(self, op: dict[str, Any], report: SyncReport) -> bool:
        uid, stamp = str(op["u"]), op["t"]
        if op.get("del"):
            if self._ts(f"pd:{uid}"):
                return False
            self._set_ts(f"pd:{uid}", stamp)
            row = self._paper_row(uid)
            self._forget_paper(uid)
            if row is None:
                return False
            self.db.delete_paper(int(row["id"]))
            report.papers_deleted += 1
            return True
        if self._ts(f"pd:{uid}"):
            return False  # deleted: deletion wins over edits
        fields = {name: value for name, value in (op.get("f") or {}).items() if name in PAPER_FIELDS}
        row = self._paper_row(uid)
        if row is None:
            if not op.get("new"):
                return self._wait(uid, op)
            twin = self._find_twin(fields)
            key = self._insert_paper(uid, fields, stamp)
            if twin is not None:  # read its key now: the collision may just have given it a suffix
                row = self.conn.execute("SELECT citation_key FROM papers WHERE id = ?", (twin,)).fetchone()
                report.duplicates.append((str(row[0]), key))
            report.papers_added += 1
            self._release(uid, report)
            return True
        accepted = self._accept(f"p:{uid}", fields, stamp)
        if not accepted:
            return False
        self._update_paper(row, accepted)
        report.papers_updated += 1
        self._release(uid, report)
        return True

    def _accept(self, prefix: str, fields: dict[str, Any], stamp: str) -> dict[str, Any]:
        """The fields whose register is older than ``stamp`` (their clocks are advanced)."""
        verdict: dict[str, bool] = {}
        accepted: dict[str, Any] = {}
        for name, value in fields.items():
            register = self._register(name) if prefix.startswith("p:") else name
            if register not in verdict:
                verdict[register] = stamp > self._ts(f"{prefix}:{register}", prefix)
                if verdict[register]:
                    self._set_ts(f"{prefix}:{register}", stamp)
            if verdict[register]:
                accepted[name] = value
        return accepted

    def _find_twin(self, fields: dict[str, Any]) -> int | None:
        """Id of a local entry that looks like the same paper as an incoming one (same PDF bytes or
        same DOI), i.e. it was added on two devices before they synchronised. Reported, never merged."""
        for column in ("sha256", "doi"):
            value = fields.get(column)
            if value:
                row = self.conn.execute(f"SELECT id FROM papers WHERE {column} = ?", (value,)).fetchone()
                if row is not None:
                    return int(row[0])
        return None

    def _forget_paper(self, uid: str) -> None:
        self._set_shadow("p", uid, None)
        self.conn.execute("DELETE FROM sync_shadow WHERE kind = 'm' AND uid LIKE ?", (f"{uid}|%",))

    def _free_key(self, fields: dict[str, Any], *, own_id: int | None) -> dict[str, Any]:
        """Resolve a citation-key (or PDF path) collision between two different papers, deterministically:
        the entry with the larger ``uid`` takes the next free suffix. Returns the fields to store."""
        key = fields.get("citation_key")
        if not key:
            return fields
        other = self.conn.execute(
            "SELECT * FROM papers WHERE (citation_key = ? OR (relative_pdf_path IS NOT NULL AND relative_pdf_path = ?))"
            " AND id IS NOT ?",
            (key, fields.get("relative_pdf_path"), own_id),
        ).fetchone()
        if other is None:
            return fields
        library = self.library
        incoming_uid = fields["_uid"]
        if other["uid"] > incoming_uid:  # the local entry gives way: an ordinary local rename (journaled later)
            paper = library.get_paper(int(other["id"]))
            new_key = library.storage.allocate_citation_key(
                _strip_suffix(paper.citation_key),
                paper.meta.year,
                lambda k: k.casefold() == str(key).casefold() or self.db.citation_key_exists(k, exclude_id=paper.id),
                own_relative_path=paper.relative_pdf_path if paper.pdf_managed else None,
            )
            try:
                library.update_metadata(paper.id, citation_key=new_key)
            except Exception as exc:  # e.g. the file is open elsewhere: rename the entry only
                log.warning("Could not rename %s to %s: %s", paper.citation_key, new_key, exc)
                self.conn.execute("UPDATE papers SET citation_key = ? WHERE id = ?", (new_key, paper.id))
            return fields
        # The incoming entry gives way here (its own device does the same and renames the file there).
        year = fields.get("year")
        managed = bool(fields.get("relative_pdf_path")) and not fields.get("pdf_linked")
        new_key = library.storage.allocate_citation_key(
            _strip_suffix(str(key)), year, lambda k: self.db.citation_key_exists(k, exclude_id=own_id)
        )
        fields = dict(fields, citation_key=new_key)
        if managed:
            fields["relative_pdf_path"] = library.storage.relative_path_for(new_key, year)
        return fields

    def _insert_paper(self, uid: str, fields: dict[str, Any], stamp: str) -> str:
        state = {name: fields.get(name) for name in PAPER_FIELDS}
        for name, default in (("citation_key", f"unnamed_{uid[:8]}"), ("entry_type", "article"), ("title", ""),
                              ("authors_json", "[]")):  # fmt: skip
            state[name] = state[name] if state[name] is not None else default
        for name in ("pdf_linked", "needs_review", "favorite", "is_read", "my_publication"):
            state[name] = int(bool(state[name]))
        self._set_ts(f"p:{uid}", stamp)
        self._set_shadow("p", uid, state)  # what the other device has; a local resolution below differs from it
        stored = self._free_key(dict(state, _uid=uid), own_id=None)
        stored.pop("_uid", None)
        if not stored.get("created_at"):
            stored.pop("created_at", None)  # database default
        columns = ["uid", *stored]
        cursor = self.conn.execute(
            f"INSERT INTO papers ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})",
            [uid, *stored.values()],
        )
        self._index(int(cursor.lastrowid or 0))
        return str(stored["citation_key"])

    def _update_paper(self, row: sqlite3.Row, accepted: dict[str, Any]) -> None:
        uid = row["uid"]
        shadow = self._shadow("p", uid) or {name: row[name] for name in PAPER_FIELDS}
        shadow.update(accepted)
        self._set_shadow("p", uid, shadow)
        values = dict(accepted)
        if "citation_key" in values:
            current = {name: row[name] for name in ("year", "relative_pdf_path", "pdf_linked")}
            resolved = self._free_key({**current, **values, "_uid": uid}, own_id=int(row["id"]))
            values = {name: resolved[name] for name in values}
        try:
            self._write_paper(int(row["id"]), values)
        except sqlite3.IntegrityError:  # should not happen (only the key is unique, and it was resolved above)
            groups = [{n: v for n, v in values.items() if n in FILE_GROUP}] + [
                {n: v} for n, v in values.items() if n not in FILE_GROUP
            ]
            for group in groups:
                if not group:
                    continue
                try:
                    self._write_paper(int(row["id"]), group)
                except sqlite3.IntegrityError:
                    log.warning("Kept the local value of %s for %s (conflict)", sorted(group), row["citation_key"])
        if _META_COLUMNS & values.keys():
            self._index(int(row["id"]))

    def _write_paper(self, paper_id: int, values: dict[str, Any]) -> None:
        if not values:
            return
        assignments = ", ".join(f"{name} = ?" for name in values)
        self.conn.execute("SAVEPOINT sync_write")
        try:
            self.conn.execute(f"UPDATE papers SET {assignments} WHERE id = ?", [*values.values(), paper_id])
        except sqlite3.IntegrityError:
            self.conn.execute("ROLLBACK TO sync_write")
            self.conn.execute("RELEASE sync_write")
            raise
        self.conn.execute("RELEASE sync_write")

    def _index(self, paper_id: int) -> None:
        row = self.conn.execute("SELECT title, authors_json, abstract FROM papers WHERE id = ?", (paper_id,)).fetchone()
        if row is not None:
            meta = PaperMetadata(
                title=row["title"] or "", authors=authors_from_json(row["authors_json"]), abstract=row["abstract"] or ""
            )
            self.db._update_fts_metadata(paper_id, meta)

    # -- folders
    def _collection_row(self, uid: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM collections WHERE uid = ?", (uid,)).fetchone()

    def _apply_collection(self, op: dict[str, Any], report: SyncReport) -> bool:
        uid, stamp = str(op["u"]), op["t"]
        if op.get("del"):
            if self._ts(f"cd:{uid}"):
                return False
            self._set_ts(f"cd:{uid}", stamp)
            row = self._collection_row(uid)
            if row is None:
                self._set_shadow("c", uid, None)
                return False
            doomed = [self.db.get_collection(cid) for cid in self.db.descendant_collection_ids(int(row["id"]))]
            uids = {c.id: self.conn.execute("SELECT uid FROM collections WHERE id = ?", (c.id,)).fetchone()[0]
                    for c in doomed if c is not None}  # fmt: skip
            self.db.delete_collection(int(row["id"]))
            for collection in doomed:
                if collection is None:
                    continue
                self._set_shadow("c", uids[collection.id], None)
                self.conn.execute("DELETE FROM sync_shadow WHERE kind = 'm' AND uid LIKE ?", (f"%|{uids[collection.id]}",))
                if self.library.config.write_bib_files:
                    self.library.bib.remove_collection_file(collection.slug)
            return True
        if self._ts(f"cd:{uid}"):
            return False
        fields = {name: value for name, value in (op.get("f") or {}).items() if name in COLLECTION_FIELDS}
        row = self._collection_row(uid)
        parent_uid = fields.get("parent")
        parent_id: int | None = None
        if parent_uid:
            parent = self._collection_row(str(parent_uid))
            if parent is None:
                return False if self._ts(f"cd:{parent_uid}") else self._wait(str(parent_uid), op)
            parent_id = int(parent["id"])
        if row is None:
            if not op.get("new"):
                return self._wait(uid, op)
            name = str(fields.get("name") or "Folder")
            slug = str(fields.get("slug") or "")
            self._set_ts(f"c:{uid}", stamp)
            self._set_shadow("c", uid, {n: fields.get(n) for n in COLLECTION_FIELDS})
            if not slug or self.db.slug_exists(slug) or slug == "master":
                slug = self.library._unique_slug(name)  # a local resolution, journaled by the next push
            self.conn.execute(
                "INSERT INTO collections (name, slug, parent_id, uid" + (", created_at" if fields.get("created_at") else "")
                + ") VALUES (?, ?, ?, ?" + (", ?" if fields.get("created_at") else "") + ")",
                [name, slug, parent_id, uid] + ([fields["created_at"]] if fields.get("created_at") else []),
            )
            self._release(uid, report)
            return True
        accepted = self._accept(f"c:{uid}", fields, stamp)
        if not accepted:
            return False
        shadow = self._shadow("c", uid) or {}
        shadow.update(accepted)
        self._set_shadow("c", uid, shadow)
        values: dict[str, Any] = {}
        if "name" in accepted:
            values["name"] = accepted["name"]
        if "slug" in accepted and accepted["slug"] != row["slug"]:
            slug = str(accepted["slug"])
            if self.db.slug_exists(slug, exclude_id=int(row["id"])) or slug == "master":
                slug = self.library._unique_slug(str(accepted.get("name") or row["name"]), exclude_id=int(row["id"]))
            if self.library.config.write_bib_files:
                self.library.bib.remove_collection_file(row["slug"])
            values["slug"] = slug
        if "parent" in accepted and parent_id not in self.db.descendant_collection_ids(int(row["id"])):
            values["parent_id"] = parent_id
        if values:
            self.db.update_collection(int(row["id"]), **values)
        return True

    # -- supplementary files
    def _apply_attachment(self, op: dict[str, Any]) -> bool:
        uid, stamp = str(op["u"]), op["t"]
        if op.get("del"):
            if self._ts(f"ad:{uid}"):
                return False
            self._set_ts(f"ad:{uid}", stamp)
            self._set_shadow("a", uid, None)
            # Files are the sync tool's business: the row goes, the file (if managed) is removed by its owner.
            return self.conn.execute("DELETE FROM attachments WHERE uid = ?", (uid,)).rowcount > 0
        if self._ts(f"ad:{uid}"):
            return False
        fields = {name: value for name, value in (op.get("f") or {}).items() if name in ATTACHMENT_FIELDS}
        row = self.conn.execute("SELECT * FROM attachments WHERE uid = ?", (uid,)).fetchone()
        if row is None:
            if not op.get("new"):
                return self._wait(uid, op)
            paper_uid = str(fields.get("paper") or "")
            if self._ts(f"pd:{paper_uid}"):
                return False
            paper = self._paper_row(paper_uid)
            if paper is None:
                return self._wait(paper_uid, op)
            state = {name: fields.get(name) for name in ATTACHMENT_FIELDS}
            state["linked"] = int(bool(state["linked"]))
            state["position"] = int(state["position"] or 1)
            state["label"] = state["label"] or ""
            self._set_ts(f"a:{uid}", stamp)
            self._set_shadow("a", uid, state)
            stored = self._free_attachment_path(state, uid, int(paper["id"]), own_id=None)
            columns = {name: stored[name] for name in ("relative_path", "linked", "sha256", "label", "position")}
            if stored.get("created_at"):
                columns["created_at"] = stored["created_at"]
            names = ["uid", "paper_id", *columns]
            self.conn.execute(
                f"INSERT INTO attachments ({', '.join(names)}) VALUES ({', '.join('?' for _ in names)})",
                [uid, int(paper["id"]), *columns.values()],
            )
            return True
        accepted: dict[str, Any] = {}
        verdict: dict[str, bool] = {}
        for name, value in fields.items():
            if name == "paper":
                continue  # a supplementary file does not change paper
            register = self._attachment_register(name)
            if register not in verdict:
                verdict[register] = stamp > self._ts(f"a:{uid}:{register}", f"a:{uid}")
                if verdict[register]:
                    self._set_ts(f"a:{uid}:{register}", stamp)
            if verdict[register]:
                accepted[name] = value
        if not accepted:
            return False
        shadow = self._shadow("a", uid) or {name: row[name] for name in ATTACHMENT_FIELDS if name != "paper"}
        shadow.update(accepted)
        self._set_shadow("a", uid, shadow)
        if "relative_path" in accepted:
            current = {name: row[name] for name in ATTACHMENT_FILE_GROUP}
            resolved = self._free_attachment_path({**current, **accepted}, uid, int(row["paper_id"]), own_id=int(row["id"]))
            accepted = {name: resolved[name] for name in accepted}
        if "linked" in accepted:
            accepted["linked"] = int(bool(accepted["linked"]))
        assignments = ", ".join(f"{name} = ?" for name in accepted)
        self.conn.execute(f"UPDATE attachments SET {assignments} WHERE id = ?", [*accepted.values(), int(row["id"])])
        return True

    def _free_attachment_path(
        self, fields: dict[str, Any], uid: str, paper_id: int, *, own_id: int | None
    ) -> dict[str, Any]:
        """Two supplementary files recorded at one managed path (added on two devices before they synchronised):
        the one with the larger ``uid`` takes the next free number, on every device alike (as for citation keys).
        Returns the fields to store."""
        relative = fields.get("relative_path")
        if not relative or fields.get("linked"):
            return fields
        other = self.conn.execute(
            "SELECT * FROM attachments WHERE relative_path = ? COLLATE NOCASE AND id IS NOT ?", (relative, own_id)
        ).fetchone()
        if other is None:
            return fields
        library = self.library
        if other["uid"] > uid:  # the local entry gives way: an ordinary local rename (journaled by the next push)
            attachment = library.get_attachment(int(other["id"]))
            paper = library.get_paper(attachment.paper_id)
            source = library.storage.absolute_path(attachment.relative_path)
            position, wanted = library._free_supplement_slot(
                paper, attachment.extension, attachment.position + 1, exclude_id=attachment.id
            )
            if source.is_file():
                try:
                    library.storage.move_relative(attachment.relative_path, wanted)
                except OSError as exc:
                    log.warning("Could not rename %s: %s", attachment.relative_path, exc)
            self.conn.execute(
                "UPDATE attachments SET relative_path = ?, position = ? WHERE id = ?", (wanted, position, attachment.id)
            )
            return fields
        # The incoming file gives way here (its own device does the same and renames the file there).
        paper = library.get_paper(paper_id)
        extension = relative[relative.rfind(".") :] if "." in relative.rsplit("/", 1)[-1] else ""
        position, wanted = library._free_supplement_slot(
            paper, extension, int(fields.get("position") or 1) + 1, exclude_id=own_id
        )
        return dict(fields, relative_path=wanted, position=position)

    # -- folder membership
    def _apply_member(self, op: dict[str, Any]) -> bool:
        paper_uid, collection_uid, stamp = str(op["p"]), str(op["c"]), op["t"]
        if self._ts(f"pd:{paper_uid}") or self._ts(f"cd:{collection_uid}"):
            return False
        paper = self._paper_row(paper_uid)
        if paper is None:
            return self._wait(paper_uid, op)
        collection = self._collection_row(collection_uid)
        if collection is None:
            return self._wait(collection_uid, op)
        pair = f"{paper_uid}|{collection_uid}"
        if stamp <= self._ts(f"m:{pair}"):
            return False
        self._set_ts(f"m:{pair}", stamp)
        if op.get("on"):
            self.db.add_paper_to_collection(int(paper["id"]), int(collection["id"]))
            self._set_shadow("m", pair, {})
        else:
            self.db.remove_paper_from_collection(int(paper["id"]), int(collection["id"]))
            self._set_shadow("m", pair, None)
        return True

    # ------------------------------------------------------------------ information for the user interface
    def devices(self) -> list[DeviceInfo]:
        found = []
        for directory in self._device_dirs():
            info = _read_json(directory / DEVICE_FILE)
            newest = max((p.stat().st_mtime for p in self._segments(directory)), default=0.0)
            found.append(
                DeviceInfo(
                    device_id=directory.name,
                    name=str(info.get("name") or directory.name),
                    app=str(info.get("app") or ""),
                    is_self=directory.name == self.device_id,
                    last_change=newest,
                )
            )
        return sorted(found, key=lambda d: (not d.is_self, d.name.casefold()))

    def conflict_copies(self) -> list[Path]:
        """Files the sync client set aside as conflicting versions (journals should never have any)."""
        hits = []
        for path in self.dir.rglob("*"):
            lowered = path.name.lower()
            if path.is_file() and "conflict" in lowered:
                hits.append(path)
        return hits


def _dumps(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


_SUFFIX = re.compile(r"^(.*\d{4})[a-z]{1,2}$")


def _strip_suffix(key: str) -> str:
    """``Smith_2020a`` -> ``Smith_2020`` (the base a collision suffix is added to)."""
    match = _SUFFIX.match(key)
    return match.group(1) if match else key


__all__ = [
    "DeviceInfo",
    "SyncEngine",
    "SyncError",
    "SyncReport",
    "device_identity",
    "is_shared",
    "library_id",
    "local_database_path",
    "prepare_database",
    "sync_dir",
]
