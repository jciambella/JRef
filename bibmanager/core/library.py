"""Library facade: orchestrates database, physical storage and BibTeX synchronisation.

This is the only object the GUI should talk to. Every mutating method leaves the three
stores consistent (SQLite row <-> single physical PDF <-> affected ``.bib`` files).

Threading contract (Milestone 3): ``prepare_import`` / ``prepare_reference`` perform the slow work
(hashing, PyMuPDF, HTTP) without touching SQLite and may run in a ``QRunnable``; every other method
must be called from the thread that created the ``Library``.

PDF ownership: *managed* PDFs live in ``pdfs/<YYYY>/<key>.pdf`` and follow the key; *linked* PDFs
(``Paper.pdf_linked``, e.g. a Mendeley library imported in place) are never renamed, moved or deleted
and their keys only change on an explicit edit; references may also have no PDF at all.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import sqlite3
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from functools import partial
from pathlib import Path
from typing import Any

from . import sync as device_sync
from .bibtex_manager import BIBTEX_DIRNAME, MASTER_SLUG, BibtexManager, is_generated_bib, render_entry, slugify
from .config import AppConfig
from .database import Database
from .extractor import MetadataResolver, PdfContent, PdfReadError, clean_doi, read_pdf
from .models import (
    Attachment,
    Collection,
    Paper,
    PaperMetadata,
    SearchHit,
    coerce_authors,
    coerce_year,
    split_list_field,
)
from .storage import (
    PdfStorage,
    base_citation_key,
    key_matches_base,
    provisional_key,
    same_file,
    sanitize_citation_key,
    sha256_file,
    validate_citation_key,
)

log = logging.getLogger(__name__)

DB_FILENAME = "library.db"
_META_FIELDS = frozenset(
    {
        "title",
        "authors",
        "year",
        "journal",
        "volume",
        "issue",
        "pages",
        "publisher",
        "doi",
        "arxiv_id",
        "entry_type",
        "abstract",
        "url",
        "isbn",
        "issn",
        "pmid",
        "keywords",
    }
)
_BOOKKEEPING_FIELDS = frozenset({"citation_key", "needs_review", "notes"})


class LibraryError(RuntimeError):
    pass


class DuplicateDoiError(LibraryError):
    pass


class ForeignBibFileError(LibraryError):
    """Refusal to overwrite a hand-written ``.bib`` that JRef did not generate."""


class IngestStatus(StrEnum):
    IMPORTED = "imported"
    DUPLICATE_HASH = "duplicate_hash"  # same bytes already in the library
    DUPLICATE_DOI = "duplicate_doi"  # different file, same DOI (e.g. re-downloaded copy)
    DUPLICATE_KEY = "duplicate_key"  # reference import: same citation key and title already present


FileMode = str  # "link" (leave the PDF where it is) | "copy" | "move" (into pdfs/<YYYY>/)
FILE_MODES = ("link", "copy", "move")
_TITLE_NORM = re.compile(r"[^0-9a-z]+")
_WINDOWS_ABSOLUTE = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\)")


def _norm_title(title: str) -> str:
    return _TITLE_NORM.sub("", (title or "").lower())


@dataclass(slots=True)
class ImportCandidate:
    """Result of the slow, thread-safe half of an import."""

    source_path: Path
    sha256: str
    metadata: PaperMetadata
    needs_review: bool
    full_text: str = ""
    source: str = "manual"
    log: list[str] = field(default_factory=list)
    duplicate: bool = False  # already in the library (detected in the worker, nothing was resolved)


@dataclass(slots=True)
class IngestResult:
    status: IngestStatus
    paper: Paper
    collection_id: int | None = None
    log: list[str] = field(default_factory=list)
    attached_pdf: bool = False  # reference import: a PDF was attached to an entry that had none
    supplements: int = 0  # reference import: supplementary files attached


@dataclass(slots=True)
class AttachmentCandidate:
    """A PDF about to be attached to an existing entry (hashed and read in a worker thread)."""

    path: Path
    sha256: str
    full_text: str = ""
    log: list[str] = field(default_factory=list)


MISSING_FILE_FIELD = "file"  # extra_fields key: attachment path(s) recorded by Mendeley that were not found


@dataclass(slots=True)
class ReferenceCandidate:
    """A reference from another manager's export (BibTeX entry + optional PDF), ready to commit.

    ``folders`` holds folder paths, outermost first (``("Thesis", "Chapter 2")``). Produced by
    ``bibmanager.core.mendeley`` and completed by ``Library.prepare_reference`` in a worker thread.
    """

    key: str
    metadata: PaperMetadata
    pdf_path: Path | None = None
    sha256: str | None = None
    full_text: str = ""
    folders: list[tuple[str, ...]] = field(default_factory=list)
    tags: str = ""
    notes: str = ""
    favorite: bool = False
    is_read: bool = False
    missing_files: list[str] = field(default_factory=list)  # recorded attachments that were not found
    extra_files: list[str] = field(default_factory=list)  # further files (attached as supplementary files)
    extra_attachments: list[AttachmentCandidate] = field(default_factory=list)  # prepared by prepare_reference
    log: list[str] = field(default_factory=list)


class Library:
    def __init__(self, config: AppConfig, *, resolver: MetadataResolver | None = None) -> None:
        self.config = config
        self.root = Path(config.library_root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.storage = PdfStorage(self.root)
        self.storage.ensure_layout()
        # A library shared between devices keeps this device's database outside the synced folder (core/sync.py).
        self.shared = config.sync_enabled or device_sync.is_shared(self.root)
        self.db_path = device_sync.prepare_database(self.root, config) if self.shared else self.root / DB_FILENAME
        self.db = Database(self.db_path)
        self.sync: device_sync.SyncEngine | None = None
        self.bib = BibtexManager(
            self.root / BIBTEX_DIRNAME, config.protected_words, include_abstract=config.include_abstract_in_bib
        )
        self.bib.bibtex_dir.mkdir(parents=True, exist_ok=True)
        self._resolver = resolver
        self._owns_resolver = False
        self._bulk_depth = 0
        self._dirty_collections: dict[int, None] = {}
        self._dirty_master = False
        self._relativize_linked_paths()
        if self.shared:
            self.sync = device_sync.SyncEngine(self)

    @classmethod
    def open(cls, config_path: str | Path | None = None, **overrides: Any) -> Library:
        return cls(AppConfig.load(config_path, **overrides))

    @property
    def resolver(self) -> MetadataResolver:
        if self._resolver is None:
            self._resolver = MetadataResolver.from_config(self.config)
            self._owns_resolver = True
        return self._resolver

    def close(self) -> None:
        self.db.close()
        if self._owns_resolver and self._resolver is not None:
            self._resolver.close()

    def __enter__(self) -> Library:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ================================================================== queries
    def get_paper(self, paper_id: int) -> Paper:
        paper = self.db.get_paper(paper_id)
        if paper is None:
            raise KeyError(f"No paper with id {paper_id}")
        return paper

    def papers(self, collection_id: int | None = None) -> list[Paper]:
        return self.db.list_papers() if collection_id is None else self.db.papers_in_collection(collection_id)

    def papers_needing_review(self) -> list[Paper]:
        return self.db.papers_needing_review()

    def favorite_papers(self) -> list[Paper]:
        return self.db.favorite_papers()

    def my_publications(self) -> list[Paper]:
        return self.db.my_publications()

    def recently_added(self, limit: int = 50) -> list[Paper]:
        return self.db.recently_added(limit)

    def recently_read(self, limit: int = 50) -> list[Paper]:
        return self.db.recently_read(limit)

    def counts(self) -> dict[str, int]:
        return self.db.counts()

    def folder_names_by_paper(self) -> dict[int, list[str]]:
        return self.db.collection_names_by_paper()

    def bibtex_entry(self, paper: Paper) -> str:
        """The exact entry that the ``.bib`` files contain (for "Copy BibTeX")."""
        return render_entry(paper, self.bib.protected_words, include_abstract=self.bib.include_abstract)

    def known_sha256(self, sha256: str) -> bool:
        """Thread-safe duplicate check: opens a private read-only connection."""
        uri = f"{Path(self.db_path).resolve().as_uri()}?mode=ro"
        with sqlite3.connect(uri, uri=True) as conn:
            row = conn.execute(
                "SELECT 1 FROM papers WHERE sha256 = :h UNION ALL SELECT 1 FROM attachments WHERE sha256 = :h LIMIT 1",
                {"h": sha256},
            ).fetchone()
            return row is not None

    def unassigned_papers(self) -> list[Paper]:
        return self.db.unassigned_papers()

    def collections(self) -> list[Collection]:
        return self.db.list_collections()

    def collections_of(self, paper_id: int) -> list[Collection]:
        return self.db.collections_of_paper(paper_id)

    def search_fulltext(self, query: str, *, limit: int = 200) -> list[SearchHit]:
        return self.db.search_fulltext(query, limit=limit)

    def search_metadata(self, text: str) -> list[Paper]:
        return self.db.search_metadata(text)

    def pdf_path(self, paper: Paper) -> Path | None:
        """Absolute path of the paper's PDF (managed or linked); ``None`` for a reference without PDF."""
        if paper.relative_pdf_path is None:
            return None
        if paper.pdf_linked:
            return self.resolve_linked_path(paper.relative_pdf_path)
        return self.storage.absolute_path(paper.relative_pdf_path)

    def resolve_linked_path(self, recorded: str) -> Path:
        """Where a linked PDF is on *this* device.

        A path inside the library folder is recorded relative to it and works everywhere. An absolute path was
        recorded on one device (``C:\\Users\\...``); on another device it is translated through
        ``config.linked_path_map`` (recorded folder -> local folder), e.g. the same synced papers folder.
        """
        windows_style = bool(_WINDOWS_ABSOLUTE.match(recorded))
        if not windows_style and not recorded.startswith("/"):
            return self.root / recorded
        if Path(recorded).is_absolute() and Path(recorded).exists():
            return Path(recorded)
        normalised = recorded.replace("\\", "/")
        for source, target in self.config.linked_path_map.items():
            prefix = source.replace("\\", "/").rstrip("/")
            head, tail = normalised[: len(prefix)], normalised[len(prefix) :]
            same = head.casefold() == prefix.casefold() if windows_style else head == prefix
            if prefix and same and (not tail or tail.startswith("/")):
                return Path(target) / tail.lstrip("/") if tail else Path(target)
        return Path(recorded)

    def pdf_available(self, paper: Paper) -> bool:
        """False for a reference without PDF and for a PDF that is not on this device (not synchronised yet,
        or linked in a folder this device has no mapping for)."""
        path = self.pdf_path(paper)
        try:
            return path is not None and path.is_file()
        except OSError:
            return False

    def unresolved_link_folders(self, *, limit: int = 12) -> list[tuple[str, int]]:
        """Folders (as recorded by another device) holding linked PDFs that cannot be found here, with the
        number of PDFs below each: what the user maps to a local folder in ``config.linked_path_map``."""
        return self.linked_folders_outside(only_missing=True, limit=limit)

    def linked_folders_outside(self, *, only_missing: bool = False, limit: int = 12) -> list[tuple[str, int]]:
        """Folders outside the library folder that hold linked PDFs (recorded with an absolute path), most
        PDFs first. Such PDFs reach another device only if that folder is synchronised too and mapped there --
        or if the library folder is (a parent of) the folder where the PDFs are."""
        missing: list[str] = []
        for (recorded,) in self.db.conn.execute(
            "SELECT relative_pdf_path FROM papers WHERE pdf_linked = 1 AND relative_pdf_path IS NOT NULL"
        ):
            if not (_WINDOWS_ABSOLUTE.match(recorded) or recorded.startswith("/")):
                continue
            if not only_missing or not self.resolve_linked_path(recorded).is_file():
                missing.append(recorded.replace("\\", "/"))
        groups: dict[str, list[str]] = {}
        for path in missing:
            groups.setdefault(path.split("/")[0].casefold(), []).append(path)
        folders = []
        for paths in groups.values():
            parents = [p.rsplit("/", 1)[0] for p in paths]
            common = parents[0]
            for parent in parents[1:]:
                while common and not (parent + "/").casefold().startswith((common + "/").casefold()):
                    common = common.rsplit("/", 1)[0] if "/" in common else ""
            folders.append((common or paths[0].split("/")[0], len(paths)))
        return sorted(folders, key=lambda item: -item[1])[:limit]

    def _relativize_linked_paths(self) -> None:
        """Record linked PDFs that lie inside the library folder relative to it, so that every device sharing
        the folder finds them (e.g. the library folder is the folder where the PDFs already were)."""
        root = os.path.normcase(os.path.abspath(self.root))
        rows = self.db.conn.execute(
            "SELECT id, relative_pdf_path FROM papers WHERE pdf_linked = 1 AND relative_pdf_path IS NOT NULL"
        ).fetchall()
        for paper_id, recorded in rows:
            if not os.path.isabs(recorded):
                continue
            absolute = os.path.abspath(recorded)
            if os.path.normcase(absolute).startswith(root + os.sep):
                relative = Path(absolute[len(root) + 1 :]).as_posix()
                try:
                    self.db.conn.execute("UPDATE papers SET relative_pdf_path = ? WHERE id = ?", (relative, paper_id))
                except sqlite3.IntegrityError:  # pragma: no cover - two entries on one file
                    log.warning("Could not make %s relative to the library folder", recorded)
        for attachment_id, recorded in self.db.conn.execute(
            "SELECT id, relative_path FROM attachments WHERE linked = 1"
        ).fetchall():
            if os.path.isabs(recorded):
                absolute = os.path.abspath(recorded)
                if os.path.normcase(absolute).startswith(root + os.sep):
                    relative = Path(absolute[len(root) + 1 :]).as_posix()
                    self.db.conn.execute("UPDATE attachments SET relative_path = ? WHERE id = ?", (relative, attachment_id))

    def linked_path_value(self, path: Path) -> str:
        """How a linked file is recorded: POSIX path relative to the library root when inside it (portable
        across machines, e.g. a synced folder), absolute otherwise."""
        absolute = Path(os.path.abspath(path))
        try:
            return absolute.relative_to(Path(os.path.abspath(self.root))).as_posix()
        except ValueError:
            return str(absolute)

    # ================================================================== collections
    def _require_collection(self, collection_id: int) -> Collection:
        collection = self.db.get_collection(collection_id)
        if collection is None:
            raise KeyError(f"No collection with id {collection_id}")
        return collection

    def _unique_slug(self, name: str, *, exclude_id: int | None = None) -> str:
        base = slugify(name)
        slug, n = base, 2
        while slug == MASTER_SLUG or self.db.slug_exists(slug, exclude_id=exclude_id):
            slug, n = f"{base}_{n}", n + 1
        return slug

    def create_collection(
        self, name: str, *, parent_id: int | None = None, bib_export_path: str | Path | None = None
    ) -> Collection:
        name = " ".join(name.split())
        if not name:
            raise ValueError("Collection name must not be empty")
        if parent_id is not None:
            self._require_collection(parent_id)
        collection = self.db.create_collection(name, self._unique_slug(name), parent_id=parent_id)
        if bib_export_path is not None:
            return self.set_collection_export_path(collection.id, bib_export_path)
        self._write_collection(collection)
        return collection

    def rename_collection(self, collection_id: int, new_name: str) -> Collection:
        old = self._require_collection(collection_id)
        new_name = " ".join(new_name.split())
        if not new_name:
            raise ValueError("Collection name must not be empty")
        slug = old.slug if slugify(new_name) == slugify(old.name) else self._unique_slug(new_name, exclude_id=old.id)
        self.db.update_collection(collection_id, name=new_name, slug=slug)
        if slug != old.slug:
            self.bib.remove_collection_file(old.slug)
        collection = self._require_collection(collection_id)
        self._write_collection(collection)
        return collection

    def set_collection_export_path(
        self, collection_id: int, path: str | Path | None, *, overwrite_foreign: bool = False
    ) -> Collection:
        """Bind (or unbind with ``None``) an external ``.bib`` target, e.g. inside a manuscript repo."""
        self._require_collection(collection_id)
        if path is None:
            self.db.update_collection(collection_id, bib_export_path=None)
            return self._require_collection(collection_id)
        target = Path(path).expanduser()
        if not target.is_absolute():
            raise ValueError("The external .bib path must be absolute")
        if target.suffix.lower() != ".bib":
            raise ValueError(f"Refusing to write BibTeX into a non-.bib file: {target}")
        if not target.parent.is_dir():
            raise FileNotFoundError(f"Folder does not exist: {target.parent}")
        if target.exists() and not is_generated_bib(target) and not overwrite_foreign:
            raise ForeignBibFileError(
                f"{target} was not generated by JRef; pass overwrite_foreign=True to replace it"
            )
        self.db.update_collection(collection_id, bib_export_path=str(target))
        collection = self._require_collection(collection_id)
        self._write_collection(collection)
        return collection

    def delete_collection(self, collection_id: int) -> None:
        """Delete a project folder and its sub-folders. Papers and PDFs are untouched."""
        self._require_collection(collection_id)
        slugs = [
            c.slug for cid in self.db.descendant_collection_ids(collection_id) if (c := self.db.get_collection(cid))
        ]
        self.db.delete_collection(collection_id)
        for slug in slugs:
            self.bib.remove_collection_file(slug)

    # ================================================================== membership (M:N)
    def add_to_collection(self, paper_id: int, collection_id: int) -> bool:
        """Link an existing paper to a project folder; the physical PDF is never copied."""
        self.get_paper(paper_id)
        self._require_collection(collection_id)
        added = self.db.add_paper_to_collection(paper_id, collection_id)
        if added:
            self._sync_bibs([collection_id], master=False)
        return added

    def remove_from_collection(self, paper_id: int, collection_id: int) -> bool:
        """Unlink only: the paper stays in the library and its PDF stays on disk."""
        removed = self.db.remove_paper_from_collection(paper_id, collection_id)
        if removed:
            self._sync_bibs([collection_id], master=False)
        return removed

    # ================================================================== import pipeline
    def prepare_import(
        self,
        source: str | Path,
        *,
        metadata: PaperMetadata | None = None,
        sha256: str | None = None,
        check_duplicate: bool = False,
    ) -> ImportCandidate:
        """Hash + PyMuPDF + metadata lookup, safe in a worker thread (no shared SQLite connection).

        With ``check_duplicate`` a file already in the library short-circuits before any
        PDF parsing or HTTP request; ``commit_import`` then simply links it.
        """
        source = Path(source)
        if not source.is_file():
            raise FileNotFoundError(source)
        digest = sha256 or sha256_file(source)
        if check_duplicate and self.known_sha256(digest):
            return ImportCandidate(source, digest, PaperMetadata(), False, source="library", duplicate=True)
        messages: list[str] = []
        try:
            content = read_pdf(source)
        except PdfReadError as exc:
            content = PdfContent(filename=source.name)
            messages.append(str(exc))
        if metadata is not None:
            meta = replace(metadata, authors=list(metadata.authors))
            return ImportCandidate(
                source, digest, meta, not meta.is_complete_for_naming(), content.full_text, "manual", messages
            )
        resolution = self.resolver.resolve(content)
        messages += resolution.log
        resolved = resolution.metadata
        if resolved is None:  # keep the best on-page title as a hint for the reviewer
            resolved = PaperMetadata(title=content.title_candidates[0] if content.title_candidates else "")
        return ImportCandidate(
            source_path=source,
            sha256=digest,
            metadata=resolved,
            needs_review=resolution.needs_review,
            full_text=content.full_text,
            source=resolution.source,
            log=messages,
        )

    def commit_import(
        self, candidate: ImportCandidate, collection_id: int | None = None, *, mode: str | None = None
    ) -> IngestResult:
        """Deduplicate, name, file into ``pdfs/<YYYY>/``, index, link, and rewrite ``.bib`` files.

        ``mode`` overrides ``config.import_mode`` (the inbox watcher always moves).
        """
        mode = mode or self.config.import_mode
        if collection_id is not None:
            self._require_collection(collection_id)
        existing = self.db.get_paper_by_sha256(candidate.sha256)
        if existing is not None:
            return self._link_existing(existing, collection_id, IngestStatus.DUPLICATE_HASH, candidate.log)
        meta = replace(candidate.metadata, doi=clean_doi(candidate.metadata.doi))
        if meta.doi:
            existing = self.db.get_paper_by_doi(meta.doi)
            if existing is not None:
                return self._link_existing(existing, collection_id, IngestStatus.DUPLICATE_DOI, candidate.log)

        base = base_citation_key(meta.authors, meta.year)
        needs_review = candidate.needs_review or base is None
        if base is None:  # unresolved: keep the original filename (sanitised) until review
            base = provisional_key(candidate.source_path.name)

        stored_rel: str | None = None
        try:
            with self.db.transaction():
                key = self.storage.allocate_citation_key(base, meta.year, self.db.citation_key_exists)
                relative = self.storage.relative_path_for(key, meta.year)
                paper_id = self.db.insert_paper(
                    sha256=candidate.sha256,
                    citation_key=key,
                    relative_pdf_path=relative,
                    meta=meta,
                    needs_review=needs_review,
                )
                self.db.index_fulltext(paper_id, meta, candidate.full_text)
                if collection_id is not None:
                    self.db.add_paper_to_collection(paper_id, collection_id)
                # Last step inside the transaction: a filesystem failure rolls the rows back.
                stored_rel = self.storage.store(candidate.source_path, key, meta.year, mode=mode)
        except BaseException:
            if stored_rel is not None:
                self._undo_store(stored_rel, candidate.source_path, mode)
            raise
        paper = self.get_paper(paper_id)
        self._sync_bibs([collection_id] if collection_id is not None else [])
        log.info("Imported %s as %s (%s)", candidate.source_path.name, paper.citation_key, candidate.source)
        return IngestResult(IngestStatus.IMPORTED, paper, collection_id, candidate.log)

    def ingest_pdf(
        self, source: str | Path, collection_id: int | None = None, *, metadata: PaperMetadata | None = None
    ) -> IngestResult:
        """Synchronous convenience wrapper (CLI, tests): ``prepare_import`` + ``commit_import``."""
        source = Path(source)
        digest = sha256_file(source)
        existing = self.db.get_paper_by_sha256(digest)
        if existing is not None:  # skip PyMuPDF and HTTP entirely for known files
            if collection_id is not None:
                self._require_collection(collection_id)
            return self._link_existing(existing, collection_id, IngestStatus.DUPLICATE_HASH, [])
        return self.commit_import(self.prepare_import(source, metadata=metadata, sha256=digest), collection_id)

    def _link_existing(
        self, paper: Paper, collection_id: int | None, status: IngestStatus, messages: list[str]
    ) -> IngestResult:
        if collection_id is not None and self.db.add_paper_to_collection(paper.id, collection_id):
            self._sync_bibs([collection_id], master=False)
        return IngestResult(status, paper, collection_id, messages)

    def _undo_store(self, relative: str, source: Path, mode: str) -> None:
        stored = self.storage.absolute_path(relative)
        try:
            if mode == "move" and not source.exists():
                shutil.move(str(stored), str(source))
            else:
                stored.unlink(missing_ok=True)
        except OSError:  # pragma: no cover - best effort
            log.exception("Could not roll back stored file %s", stored)

    # ================================================================== editing
    def update_metadata(self, paper_id: int, **changes: Any) -> Paper:
        """Edit metadata; authors/year edits rename the PDF, move it across ``<YYYY>/`` and resync ``.bib``.

        Accepted keys: bibliographic fields (``title``, ``authors`` [list[Author] | str],
        ``year``, ``journal``, ``volume``, ``issue``, ``pages``, ``publisher``, ``doi``,
        ``arxiv_id``, ``entry_type``, ``abstract``) plus ``citation_key``, ``needs_review``, ``notes``.
        """
        paper = self.get_paper(paper_id)
        unknown = set(changes) - _META_FIELDS - _BOOKKEEPING_FIELDS
        if unknown:
            raise TypeError(f"Unknown fields: {sorted(unknown)}")

        meta_changes: dict[str, Any] = {}
        for name in _META_FIELDS & set(changes):
            value = changes[name]
            if name == "authors":
                meta_changes[name] = coerce_authors(value)
            elif name == "year":
                meta_changes[name] = coerce_year(value)
            elif name == "doi":
                meta_changes[name] = clean_doi(str(value or ""))
            elif name == "keywords":
                meta_changes[name] = "; ".join(split_list_field(str(value or "")))
            else:
                meta_changes[name] = " ".join(str(value or "").split()) if name != "abstract" else str(value or "")
        meta = replace(paper.meta, **meta_changes)
        if meta.doi and meta.doi != paper.meta.doi:
            other = self.db.get_paper_by_doi(meta.doi)
            if other is not None and other.id != paper.id:
                raise DuplicateDoiError(f"DOI {meta.doi} already belongs to {other.citation_key}")

        naming_changed = "authors" in meta_changes or "year" in meta_changes
        if not paper.pdf_managed:  # linked file or no PDF: the key only changes on an explicit edit
            new_key = paper.citation_key
            if "citation_key" in changes:
                new_key = validate_citation_key(str(changes["citation_key"]).strip(), filename=False)
                if self.db.citation_key_exists(new_key, exclude_id=paper.id):
                    raise LibraryError(f"Citation key {new_key!r} is already in use")
            if "needs_review" in changes:
                needs_review = bool(changes["needs_review"])
            elif naming_changed and meta.is_complete_for_naming() and meta.title:
                needs_review = False
            else:
                needs_review = paper.needs_review
            self.db.update_paper(
                paper.id, meta=meta, citation_key=new_key, needs_review=needs_review, notes=changes.get("notes")
            )
            self._relocate_supplements(paper.id)
            self._sync_bibs(c.id for c in self.db.collections_of_paper(paper.id))
            return self.get_paper(paper.id)

        assert paper.relative_pdf_path is not None
        base = base_citation_key(meta.authors, meta.year)
        dir_changed = self.storage.year_dirname(meta.year) != self.storage.year_dirname(paper.meta.year)
        own_path = self.pdf_path(paper)

        def taken(key: str) -> bool:
            return self.db.citation_key_exists(key, exclude_id=paper.id)

        if "citation_key" in changes:
            new_key = validate_citation_key(str(changes["citation_key"]).strip())
            target = self.storage.absolute_path(self.storage.relative_path_for(new_key, meta.year))
            if taken(new_key) or (target.exists() and not same_file(target, own_path)):
                raise LibraryError(f"Citation key {new_key!r} is already in use")
        elif naming_changed and base is not None and not key_matches_base(paper.citation_key, base):
            new_key = self.storage.allocate_citation_key(
                base, meta.year, taken, own_relative_path=paper.relative_pdf_path
            )
        elif dir_changed:  # e.g. provisional key moved out of Unknown/
            new_key = self.storage.allocate_citation_key(
                paper.citation_key, meta.year, taken, own_relative_path=paper.relative_pdf_path
            )
        else:
            new_key = paper.citation_key

        if "needs_review" in changes:
            needs_review = bool(changes["needs_review"])
        elif naming_changed and base is not None:
            needs_review = False
        else:
            needs_review = paper.needs_review

        new_rel = self.storage.relative_path_for(new_key, meta.year)
        moved = False
        try:
            with self.db.transaction():
                self.db.update_paper(
                    paper.id,
                    meta=meta,
                    citation_key=new_key,
                    relative_pdf_path=new_rel,
                    needs_review=needs_review,
                    notes=changes.get("notes"),
                )
                if new_rel != paper.relative_pdf_path:
                    self.storage.relocate(paper.relative_pdf_path, new_key, meta.year)
                    moved = True
        except BaseException:
            if moved:
                self.storage.relocate(new_rel, paper.citation_key, paper.meta.year)
            raise
        self._relocate_supplements(paper.id)
        self._sync_bibs(c.id for c in self.db.collections_of_paper(paper.id))
        return self.get_paper(paper.id)

    def apply_metadata(self, paper_id: int, meta: PaperMetadata) -> Paper:
        """Merge fetched metadata (e.g. "Fetch by DOI"): non-empty fetched fields win, the rest is kept."""
        fields = {
            name: getattr(meta, name)
            for name in _META_FIELDS
            if getattr(meta, name) not in ("", None, [], {}) and not (name == "entry_type" and not meta.title)
        }
        return self.update_metadata(paper_id, **fields) if fields else self.get_paper(paper_id)

    def update_flags(
        self,
        paper_id: int,
        *,
        favorite: bool | None = None,
        is_read: bool | None = None,
        my_publication: bool | None = None,
        tags: str | None = None,
        notes: str | None = None,
    ) -> Paper:
        """Library bookkeeping (star, read dot, tags, notes): no renaming, no ``.bib`` rewrite."""
        self.db.update_paper(
            paper_id,
            favorite=favorite,
            is_read=is_read,
            my_publication=my_publication,
            tags=None if tags is None else "; ".join(split_list_field(tags)),
            notes=notes,
        )
        return self.get_paper(paper_id)

    def mark_opened(self, paper_id: int) -> Paper:
        """Record that the PDF was opened ("Recently Read"); also clears the unread dot."""
        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        self.db.update_paper(paper_id, is_read=True, last_opened_at=now)
        return self.get_paper(paper_id)

    def delete_paper(self, paper_id: int, *, delete_file: bool = True) -> None:
        """SPEC "Delete from Library": unlink from every project, drop the row, delete the managed PDF.

        Linked PDFs are never deleted: they belong to a folder structure bibmanager does not own.
        """
        paper = self.get_paper(paper_id)
        affected = [c.id for c in self.db.collections_of_paper(paper_id)]
        supplements = [a.relative_path for a in self.db.attachments_of(paper_id) if not a.linked]
        self.db.delete_paper(paper_id)  # attachments rows go with it (ON DELETE CASCADE)
        if delete_file and paper.pdf_managed:
            assert paper.relative_pdf_path is not None
            self.storage.delete(paper.relative_pdf_path)
        if delete_file:
            for relative in supplements:
                self.storage.delete(relative)
        self._sync_bibs(affected)

    # ================================================================== naming rule
    def expected_key_base(self, paper: Paper) -> str | None:
        return base_citation_key(paper.meta.authors, paper.meta.year)

    def papers_with_outdated_keys(self) -> list[tuple[Paper, str]]:
        """Papers whose key does not follow the current naming rule (e.g. ``anand_2023`` from v0.3, or a
        key kept from Mendeley), with the base key they would get. Papers without authors/year are skipped."""
        outdated = []
        for paper in self.db.list_papers():
            base = self.expected_key_base(paper)
            if base is not None and not key_matches_base(paper.citation_key, base):
                outdated.append((paper, base))
        return outdated

    def apply_naming_rule(self, paper_ids: Iterable[int]) -> list[tuple[str, str]]:
        """Re-key the given papers with the naming rule (managed PDFs are renamed too).

        Returns ``(old_key, new_key)`` pairs. Changing keys breaks ``\\cite{}`` commands in existing
        manuscripts, so the GUI asks for confirmation first.
        """
        renamed: list[tuple[str, str]] = []
        with self.bulk_update():
            for paper_id in paper_ids:
                paper = self.get_paper(paper_id)
                base = self.expected_key_base(paper)
                if base is None or key_matches_base(paper.citation_key, base):
                    continue
                key = self.storage.allocate_citation_key(
                    base,
                    paper.meta.year,
                    partial(self.db.citation_key_exists, exclude_id=paper.id),
                    own_relative_path=paper.relative_pdf_path if paper.pdf_managed else None,
                )
                updated = self.update_metadata(paper.id, citation_key=key)
                renamed.append((paper.citation_key, updated.citation_key))
        return renamed

    def move_into_storage(self, paper_id: int, *, mode: str = "copy") -> Paper:
        """Turn a linked PDF into a managed one: copy (default, the original stays) or move it into
        ``pdfs/<YYYY>/<key>.pdf``. The key is kept (sanitised if it is not a valid file name)."""
        paper = self.get_paper(paper_id)
        if not paper.pdf_linked:
            return paper
        source = self.pdf_path(paper)
        if source is None or not source.is_file():
            raise LibraryError(f"Linked PDF not found: {source}")
        key = paper.citation_key
        try:
            validate_citation_key(key)
        except ValueError:
            key = sanitize_citation_key(key)

        def taken(candidate: str) -> bool:
            return self.db.citation_key_exists(candidate, exclude_id=paper.id)

        key = self.storage.allocate_citation_key(key, paper.meta.year, taken)
        stored_rel: str | None = None
        try:
            with self.db.transaction():
                self.db.update_paper(
                    paper.id,
                    citation_key=key,
                    relative_pdf_path=self.storage.relative_path_for(key, paper.meta.year),
                    pdf_linked=False,
                )
                stored_rel = self.storage.store(source, key, paper.meta.year, mode=mode)
        except BaseException:
            if stored_rel is not None:
                self._undo_store(stored_rel, source, mode)
            raise
        self._relocate_supplements(paper.id)
        self._sync_bibs(c.id for c in self.db.collections_of_paper(paper.id))
        return self.get_paper(paper.id)

    # ================================================================== attaching PDFs to existing entries
    def prepare_attachment(self, path: str | Path, *, index_fulltext: bool = True) -> AttachmentCandidate:
        """Hash and read a PDF for ``attach_pdf``. Thread-safe (no shared SQLite connection)."""
        path = Path(path)
        if not path.is_file():
            raise FileNotFoundError(path)
        candidate = AttachmentCandidate(path, sha256_file(path))
        if index_fulltext and path.suffix.lower() == ".pdf":
            try:
                candidate.full_text = read_pdf(path).full_text
            except PdfReadError as exc:
                candidate.log.append(str(exc))
        return candidate

    def attach_pdf(self, paper_id: int, candidate: AttachmentCandidate, *, mode: str | None = None) -> Paper:
        """Give a PDF to an entry that has none (e.g. a reference imported from a .bib).

        ``mode``: ``"link"`` leaves the file where it is; ``"copy"``/``"move"`` (default: the configured
        import mode) file it as ``pdfs/<YYYY>/<key>.pdf``. A PDF already in the library is refused.
        """
        mode = mode or self.config.import_mode
        if mode not in FILE_MODES:
            raise ValueError(f"mode must be one of {FILE_MODES}")
        paper = self.get_paper(paper_id)
        if paper.has_pdf:
            raise LibraryError(f"{paper.citation_key} already has a PDF")
        other = self.db.get_paper_by_sha256(candidate.sha256)
        if other is not None and other.id != paper.id:
            raise LibraryError(f"This PDF is already in the library as {other.citation_key}")
        meta = paper.meta
        if MISSING_FILE_FIELD in meta.extra_fields:
            meta = replace(meta, extra_fields={k: v for k, v in meta.extra_fields.items() if k != MISSING_FILE_FIELD})
        source = Path(candidate.path)
        key = paper.citation_key
        stored_rel: str | None = None
        try:
            with self.db.transaction():
                if mode == "link":
                    self.db.update_paper(
                        paper.id,
                        meta=meta,
                        sha256=candidate.sha256,
                        relative_pdf_path=self.linked_path_value(source),
                        pdf_linked=True,
                    )
                else:
                    try:
                        validate_citation_key(key)
                    except ValueError:
                        key = sanitize_citation_key(key)
                    key = self.storage.allocate_citation_key(
                        key, meta.year, partial(self.db.citation_key_exists, exclude_id=paper.id)
                    )
                    self.db.update_paper(
                        paper.id,
                        meta=meta,
                        citation_key=key,
                        sha256=candidate.sha256,
                        relative_pdf_path=self.storage.relative_path_for(key, meta.year),
                        pdf_linked=False,
                    )
                self.db.index_fulltext(paper.id, meta, candidate.full_text)
                if mode != "link":
                    stored_rel = self.storage.store(source, key, meta.year, mode=mode)
        except BaseException:
            if stored_rel is not None:
                self._undo_store(stored_rel, source, mode)
            raise
        if key != paper.citation_key:
            self._relocate_supplements(paper.id)
            self._sync_bibs(c.id for c in self.db.collections_of_paper(paper.id))
        log.info("Attached %s to %s (%s)", source.name, key, mode)
        return self.get_paper(paper.id)

    def _record_missing_files(self, paper: Paper, files: Sequence[str]) -> None:
        if paper.meta.extra_fields.get(MISSING_FILE_FIELD):
            return
        extra = {**paper.meta.extra_fields, MISSING_FILE_FIELD: "; ".join(files)}
        self.db.update_paper(paper.id, meta=replace(paper.meta, extra_fields=extra))

    def missing_pdf_hints(self, paper: Paper) -> list[str]:
        """Attachment paths recorded at import (e.g. by Mendeley) that were not found."""
        return split_list_field(paper.meta.extra_fields.get(MISSING_FILE_FIELD, ""))

    def papers_without_pdf(self) -> list[Paper]:
        return [p for p in self.db.list_papers() if not p.has_pdf]

    # ================================================================== supplementary files
    def attachments(self, paper_id: int) -> list[Attachment]:
        """Supplementary files of a paper (the main PDF is ``Paper.relative_pdf_path``), in display order."""
        return self.db.attachments_of(paper_id)

    def get_attachment(self, attachment_id: int) -> Attachment:
        attachment = self.db.get_attachment(attachment_id)
        if attachment is None:
            raise KeyError(f"No supplementary file with id {attachment_id}")
        return attachment

    def attachment_path(self, attachment: Attachment) -> Path:
        if attachment.linked:
            return self.resolve_linked_path(attachment.relative_path)
        return self.storage.absolute_path(attachment.relative_path)

    def attachment_available(self, attachment: Attachment) -> bool:
        try:
            return self.attachment_path(attachment).is_file()
        except OSError:
            return False

    @staticmethod
    def _file_stem(citation_key: str) -> str:
        """The citation key as a file-name stem (keys imported from other managers may contain ``:`` etc.)."""
        try:
            return validate_citation_key(citation_key)
        except ValueError:
            return sanitize_citation_key(citation_key)

    def _free_supplement_slot(
        self, paper: Paper, extension: str, start: int, *, exclude_id: int | None = None, own: Path | None = None
    ) -> tuple[int, str]:
        """First position >= ``start`` whose managed path is free in the database and on disk."""
        stem = self._file_stem(paper.citation_key)
        position = max(1, start)
        while True:
            relative = self.storage.supplement_path_for(stem, paper.meta.year, position, extension)
            target = self.storage.absolute_path(relative)
            taken = self.db.attachment_path_taken(relative, exclude_id=exclude_id)
            if not taken and (not target.exists() or same_file(target, own)):
                return position, relative
            position += 1

    def add_supplement(
        self, paper_id: int, candidate: AttachmentCandidate, *, mode: str | None = None, label: str | None = None
    ) -> Attachment:
        """Attach a supplementary file (any type: PDF, spreadsheet, archive, video, ...) to a paper.

        ``mode``: ``"link"`` leaves the file where it is; ``"copy"``/``"move"`` (default: the configured import
        mode) file it as ``pdfs/<YYYY>/<key>_S<n>.<ext>`` next to the main PDF. ``label`` defaults to the original
        file name. The same bytes cannot be in the library twice (as a main PDF or a supplementary file).
        """
        mode = mode or self.config.import_mode
        if mode not in FILE_MODES:
            raise ValueError(f"mode must be one of {FILE_MODES}")
        paper = self.get_paper(paper_id)
        other = self.db.get_paper_by_sha256(candidate.sha256)
        if other is not None:
            where = "this document" if other.id == paper.id else other.citation_key
            raise LibraryError(f"{Path(candidate.path).name} is already in the library ({where})")
        source = Path(candidate.path)
        existing = self.db.attachments_of(paper.id)
        start = max((a.position for a in existing), default=0) + 1
        text = label if label is not None else source.stem
        stored_rel: str | None = None
        try:
            with self.db.transaction():
                if mode == "link":
                    position, relative, linked = start, self.linked_path_value(source), True
                else:
                    position, relative = self._free_supplement_slot(paper, source.suffix, start)
                    linked = False
                attachment_id = self.db.insert_attachment(
                    paper.id, relative_path=relative, linked=linked, sha256=candidate.sha256, label=text,
                    position=position,
                )  # fmt: skip
                self.db.append_fulltext(paper.id, paper.meta, candidate.full_text)
                if not linked:
                    stored_rel = self.storage.store_as(source, relative, mode=mode)
        except BaseException:
            if stored_rel is not None:
                self._undo_store(stored_rel, source, mode)
            raise
        log.info("Supplementary file %s attached to %s (%s)", source.name, paper.citation_key, mode)
        return self.get_attachment(attachment_id)

    def rename_supplement(self, attachment_id: int, label: str) -> Attachment:
        """Change the label shown for a supplementary file (the file name is not touched)."""
        self.get_attachment(attachment_id)
        self.db.update_attachment(attachment_id, label=" ".join(label.split()))
        return self.get_attachment(attachment_id)

    def remove_supplement(self, attachment_id: int, *, delete_file: bool = True) -> None:
        """Detach a supplementary file; a managed file is deleted, a linked one is never touched.
        The full-text entry still contains its text until ``set_fulltext`` rebuilds it."""
        attachment = self.get_attachment(attachment_id)
        self.db.delete_attachment(attachment.id)
        if delete_file and not attachment.linked:
            self.storage.delete(attachment.relative_path)

    def promote_supplement(self, attachment_id: int) -> Paper:
        """Make a supplementary PDF the main PDF of its paper; the former main PDF becomes a supplementary file
        (e.g. when the main PDF is in fact the supplementary information). Managed files swap names."""
        attachment = self.get_attachment(attachment_id)
        if not attachment.is_pdf:
            raise LibraryError("Only a PDF can be the main file of a document")
        paper = self.get_paper(attachment.paper_id)
        stem = self._file_stem(paper.citation_key)
        main_slot = self.storage.relative_path_for(stem, paper.meta.year)
        supp_path = self.attachment_path(attachment)
        if not supp_path.is_file():
            raise LibraryError(f"File not found: {supp_path}")
        moves: list[tuple[str, str]] = []  # done renames (source, destination), undone on failure

        def move(source: str, destination: str) -> None:
            self.storage.move_relative(source, destination)
            moves.append((source, destination))

        try:
            with self.db.transaction():
                if not paper.has_pdf:
                    if attachment.linked:
                        new_main, new_linked = attachment.relative_path, True
                    else:
                        if self.storage.absolute_path(main_slot).exists():
                            raise LibraryError(f"{main_slot} already exists")
                        new_main, new_linked = main_slot, False
                    self.db.update_paper(paper.id, relative_pdf_path=new_main, pdf_linked=new_linked,
                                         sha256=attachment.sha256)  # fmt: skip
                    self.db.delete_attachment(attachment.id)
                    if not attachment.linked:
                        move(attachment.relative_path, main_slot)
                else:
                    assert paper.relative_pdf_path is not None
                    old_main, old_linked, old_sha = paper.relative_pdf_path, paper.pdf_linked, paper.sha256
                    new_main = attachment.relative_path if attachment.linked else main_slot
                    if old_linked:
                        new_supp = old_main
                    else:
                        _pos, new_supp = self._free_supplement_slot(
                            paper, ".pdf", attachment.position, exclude_id=attachment.id, own=supp_path
                        )
                    self.db.update_paper(paper.id, relative_pdf_path=new_main, pdf_linked=attachment.linked,
                                         sha256=attachment.sha256)  # fmt: skip
                    self.db.update_attachment(attachment.id, relative_path=new_supp, linked=old_linked,
                                              sha256=old_sha, label="Former main PDF")  # fmt: skip
                    # Files last (a failure rolls the rows back): managed main -> its supplementary slot, then the
                    # managed supplement -> the main slot, through a temporary name when the slots are swapped.
                    temp = None
                    if not attachment.linked and not old_linked and new_supp == attachment.relative_path:
                        temp = f"{attachment.relative_path}.swap"
                        move(attachment.relative_path, temp)
                    if not old_linked:
                        move(old_main, new_supp)
                    if not attachment.linked:
                        move(temp or attachment.relative_path, main_slot)
        except BaseException:
            for source, destination in reversed(moves):
                try:
                    self.storage.move_relative(destination, source)
                except OSError:  # pragma: no cover - best effort
                    log.exception("Could not undo the rename %s -> %s", source, destination)
            raise
        return self.get_paper(paper.id)

    def _relocate_supplements(self, paper_id: int) -> None:
        """Rename managed supplementary files after the key or the year of their paper changed (best effort: a
        file that is missing, e.g. not synchronised to this device yet, keeps its recorded path)."""
        paper = self.get_paper(paper_id)
        stem = self._file_stem(paper.citation_key)
        for attachment in self.db.attachments_of(paper_id):
            if attachment.linked:
                continue
            wanted = self.storage.supplement_path_for(stem, paper.meta.year, attachment.position, attachment.extension)
            if wanted == attachment.relative_path:
                continue
            source = self.storage.absolute_path(attachment.relative_path)
            if not source.is_file():
                log.warning("Supplementary file not found, not renamed: %s", source)
                continue
            position, wanted = self._free_supplement_slot(
                paper, attachment.extension, attachment.position, exclude_id=attachment.id, own=source
            )
            try:
                self.storage.move_relative(attachment.relative_path, wanted)
            except OSError as exc:
                log.warning("Could not rename %s: %s", attachment.relative_path, exc)
                continue
            self.db.update_attachment(attachment.id, relative_path=wanted, position=position)

    def fulltext_sources(self, paper_id: int) -> list[Path]:
        """Files whose text makes up the paper's full-text entry: the main PDF and the supplementary PDFs."""
        paper = self.get_paper(paper_id)
        paths = [p] if (p := self.pdf_path(paper)) is not None and p.is_file() else []
        paths += [path for a in self.db.attachments_of(paper_id) if a.is_pdf and (path := self.attachment_path(a)).is_file()]
        return paths

    @staticmethod
    def read_fulltext(paths: Sequence[Path]) -> str:
        """Text of several PDFs (thread-safe, no SQLite): for ``set_fulltext`` after a file was removed."""
        texts = []
        for path in paths:
            try:
                texts.append(read_pdf(path).full_text)
            except PdfReadError as exc:
                log.warning("%s", exc)
        return "\n".join(texts)

    def set_fulltext(self, paper_id: int, text: str) -> None:
        self.db.index_fulltext(paper_id, self.get_paper(paper_id).meta, text)

    def _attach_extra_files(self, paper: Paper, candidate: ReferenceCandidate, file_mode: str) -> int:
        """Reference import: the further files of an entry (Mendeley keeps several per entry) become its
        supplementary files; files already in the library are skipped, so a re-import adds nothing twice."""
        attached = 0
        for extra in candidate.extra_attachments:
            if self.db.get_paper_by_sha256(extra.sha256) is not None or not Path(extra.path).is_file():
                continue
            try:
                self.add_supplement(paper.id, extra, mode=file_mode)
                attached += 1
            except (LibraryError, OSError) as exc:
                candidate.log.append(f"{Path(extra.path).name}: {exc}")
        return attached

    # ================================================================== reference import (Mendeley, .bib)
    def prepare_reference(self, candidate: ReferenceCandidate, *, index_fulltext: bool = True) -> ReferenceCandidate:
        """Hash and (optionally) extract the full text of the attached PDF and of the further files (attached as
        supplementary files). Thread-safe, no SQLite."""
        for extra in candidate.extra_files:
            try:
                candidate.extra_attachments.append(self.prepare_attachment(extra, index_fulltext=index_fulltext))
            except OSError as exc:
                candidate.log.append(f"Could not read {extra}: {exc}")
        if candidate.pdf_path is None:
            return candidate
        try:
            candidate.sha256 = sha256_file(candidate.pdf_path)
        except OSError as exc:
            candidate.log.append(f"Could not read {candidate.pdf_path}: {exc}")
            candidate.missing_files.append(str(candidate.pdf_path))
            candidate.pdf_path = None
            return candidate
        if index_fulltext and not self.known_sha256(candidate.sha256):
            try:
                candidate.full_text = read_pdf(candidate.pdf_path).full_text
            except PdfReadError as exc:
                candidate.log.append(str(exc))
        return candidate

    def ensure_collection_path(self, names: Sequence[str]) -> int:
        """Id of the folder ``names[0] / names[1] / ...``, creating missing levels (names match case-insensitively)."""
        parent: int | None = None
        for raw in names:
            name = " ".join(str(raw).split())
            if not name:
                continue
            match = next(
                (
                    c
                    for c in self.db.list_collections()
                    if c.parent_id == parent and c.name.casefold() == name.casefold()
                ),
                None,
            )
            parent = match.id if match is not None else self.create_collection(name, parent_id=parent).id
        if parent is None:
            raise ValueError("Empty folder path")
        return parent

    def _find_existing_reference(self, candidate: ReferenceCandidate) -> tuple[Paper, IngestStatus] | None:
        if candidate.sha256:
            existing = self.db.get_paper_by_sha256(candidate.sha256)
            if existing is not None:
                return existing, IngestStatus.DUPLICATE_HASH
        doi = clean_doi(candidate.metadata.doi)
        if doi:
            existing = self.db.get_paper_by_doi(doi)
            if existing is not None:
                return existing, IngestStatus.DUPLICATE_DOI
        if candidate.key:
            existing = self.db.get_paper_by_citation_key(candidate.key)
            if existing is not None and _norm_title(existing.meta.title) == _norm_title(candidate.metadata.title):
                return existing, IngestStatus.DUPLICATE_KEY
        return None

    def commit_reference(
        self,
        candidate: ReferenceCandidate,
        *,
        file_mode: FileMode = "link",
        keep_key: bool = True,
        folder_ids: Iterable[int] = (),
    ) -> IngestResult:
        """Insert one reference (GUI thread). Re-importing is idempotent: an entry already in the library
        (same PDF bytes, DOI, or key + title) only gains the folders and the favourite/read flags."""
        if file_mode not in FILE_MODES:
            raise ValueError(f"file_mode must be one of {FILE_MODES}")
        folder_ids = list(dict.fromkeys(folder_ids))
        found = self._find_existing_reference(candidate)
        if found is not None:
            existing, status = found
            for cid in folder_ids:
                if self.db.add_paper_to_collection(existing.id, cid):
                    self._sync_bibs([cid], master=False)
            if (candidate.favorite and not existing.favorite) or (candidate.is_read and not existing.is_read):
                self.db.update_paper(
                    existing.id,
                    favorite=True if candidate.favorite else None,
                    is_read=True if candidate.is_read else None,
                )
            attached = False
            if not existing.has_pdf and candidate.pdf_path is not None and candidate.sha256:
                # e.g. first import without the right PDF folder: the re-import now finds the file
                attachment = AttachmentCandidate(candidate.pdf_path, candidate.sha256, candidate.full_text)
                self.attach_pdf(existing.id, attachment, mode=file_mode)
                attached = True
            elif not existing.has_pdf and candidate.missing_files:
                self._record_missing_files(existing, candidate.missing_files)
            supplements = self._attach_extra_files(self.get_paper(existing.id), candidate, file_mode)
            result = IngestResult(status, self.get_paper(existing.id), None, candidate.log)
            result.attached_pdf = attached
            result.supplements = supplements
            return result

        meta = replace(candidate.metadata, doi=clean_doi(candidate.metadata.doi))
        pdf = candidate.pdf_path if candidate.pdf_path is not None and candidate.sha256 else None
        managed = pdf is not None and file_mode in ("copy", "move")
        if pdf is None and candidate.missing_files:  # remembered for "Find Missing PDFs"
            meta = replace(
                meta, extra_fields={**meta.extra_fields, MISSING_FILE_FIELD: "; ".join(candidate.missing_files)}
            )

        if keep_key and candidate.key:
            base = sanitize_citation_key(candidate.key, filename=managed)
        else:
            base = base_citation_key(meta.authors, meta.year) or provisional_key(
                pdf.name if pdf is not None else (meta.title[:60] or candidate.key or "unnamed")
            )
        needs_review = not meta.title or not meta.is_complete_for_naming()

        stored_rel: str | None = None
        try:
            with self.db.transaction():
                key = self.storage.allocate_citation_key(base, meta.year, self.db.citation_key_exists)
                if pdf is None:
                    relative, linked = None, False
                elif managed:
                    relative, linked = self.storage.relative_path_for(key, meta.year), False
                else:
                    relative, linked = self.linked_path_value(pdf), True
                paper_id = self.db.insert_paper(
                    sha256=candidate.sha256 if pdf is not None else None,
                    citation_key=key,
                    relative_pdf_path=relative,
                    pdf_linked=linked,
                    meta=meta,
                    needs_review=needs_review,
                    notes=candidate.notes,
                )
                self.db.update_paper(
                    paper_id,
                    favorite=candidate.favorite or None,
                    is_read=candidate.is_read or None,
                    tags="; ".join(split_list_field(candidate.tags)) or None,
                )
                self.db.index_fulltext(paper_id, meta, candidate.full_text)
                for cid in folder_ids:
                    self.db.add_paper_to_collection(paper_id, cid)
                if managed:
                    assert pdf is not None
                    stored_rel = self.storage.store(pdf, key, meta.year, mode=file_mode)
        except BaseException:
            if stored_rel is not None and pdf is not None:
                self._undo_store(stored_rel, pdf, file_mode)
            raise
        supplements = self._attach_extra_files(self.get_paper(paper_id), candidate, file_mode)
        self._sync_bibs(folder_ids)
        result = IngestResult(IngestStatus.IMPORTED, self.get_paper(paper_id), None, candidate.log)
        result.supplements = supplements
        return result

    # ================================================================== BibTeX sync
    def _write_collection(self, collection: Collection) -> list[Path]:
        if not self.config.write_bib_files:
            return []
        return self.bib.write_collection(collection, self.db.papers_in_collection(collection.id))

    def _sync_bibs(self, collection_ids: Iterable[int], *, master: bool = True) -> None:
        if not self.config.write_bib_files:
            return
        if self._bulk_depth:
            self._dirty_collections.update(dict.fromkeys(collection_ids))
            self._dirty_master = self._dirty_master or master
            return
        for collection_id in dict.fromkeys(collection_ids):
            collection = self.db.get_collection(collection_id)
            if collection is not None:
                self._write_collection(collection)
        if master and self.config.write_master_bib:
            self.bib.write_master(self.db.list_papers())

    def begin_bulk(self) -> None:
        """Defer ``.bib`` writes (large imports): each affected file is written once by ``end_bulk``."""
        self._bulk_depth += 1

    def end_bulk(self) -> None:
        if self._bulk_depth == 0:
            return
        self._bulk_depth -= 1
        if self._bulk_depth == 0:
            dirty, master = list(self._dirty_collections), self._dirty_master
            self._dirty_collections, self._dirty_master = {}, False
            if dirty or master:
                self._sync_bibs(dirty, master=master)

    @contextmanager
    def bulk_update(self) -> Iterator[None]:
        self.begin_bulk()
        try:
            yield
        finally:
            self.end_bulk()

    def sync_all_bibs(self) -> None:
        self._sync_bibs((c.id for c in self.db.list_collections()), master=True)

    # ================================================================== sharing between devices
    def enable_sharing(self) -> Library:
        """Turn this library into one shared through its (synced) folder and return the reopened library:
        the database moves to the per-device data folder and the whole content is journaled once."""
        if self.shared:
            return self
        config = replace(self.config, sync_enabled=True)
        resolver, self._resolver = self._resolver, None
        self.close()
        library = Library(config, resolver=resolver)
        library._owns_resolver = self._owns_resolver
        library.sync_now()
        return library

    def sync_now(self) -> device_sync.SyncReport:
        """Journal local changes, apply the other devices' changes, refresh the ``.bib`` files."""
        if self.sync is None:
            return device_sync.SyncReport()
        report = self.sync.sync()
        if report.changed:
            self.sync_all_bibs()
        return report

    def push_changes(self) -> int:
        """Journal local changes only (cheap; call after edits so that the sync client can upload them)."""
        return self.sync.push() if self.sync is not None else 0

    def relocate(self, new_root: str | Path) -> AppConfig:
        """Move the library to another folder (e.g. the synchronised folder where the linked PDFs already are)
        and close it; returns the configuration to reopen it with.

        Only JRef's own items move (``pdfs/``, ``bibtex/``, ``sync/``, ``inbox/``, ``library.db``); linked PDFs
        stay where they are and are found again: links inside the new folder become relative to it.
        """
        target = Path(new_root).expanduser()
        if not target.is_absolute():
            raise ValueError("The library folder must be an absolute path")
        old = Path(os.path.abspath(self.root))
        if Path(os.path.abspath(target)) == old:
            return self.config
        items = ("pdfs", "bibtex", device_sync.SYNC_DIRNAME, "inbox", DB_FILENAME)
        for name in items:
            existing = target / name
            if existing.is_file() or (existing.is_dir() and any(existing.iterdir())):
                raise LibraryError(f"{target} already contains '{name}': choose a folder that holds no other library")
        for paper_id, recorded in self.db.conn.execute(
            "SELECT id, relative_pdf_path FROM papers WHERE pdf_linked = 1 AND relative_pdf_path IS NOT NULL"
        ).fetchall():
            if not (_WINDOWS_ABSOLUTE.match(recorded) or recorded.startswith("/")):  # relative to the old folder
                absolute = str(old.joinpath(*recorded.split("/")))
                self.db.conn.execute("UPDATE papers SET relative_pdf_path = ? WHERE id = ?", (absolute, paper_id))
        for attachment_id, recorded in self.db.conn.execute(
            "SELECT id, relative_path FROM attachments WHERE linked = 1"
        ).fetchall():
            if not (_WINDOWS_ABSOLUTE.match(recorded) or recorded.startswith("/")):
                absolute = str(old.joinpath(*recorded.split("/")))
                self.db.conn.execute("UPDATE attachments SET relative_path = ? WHERE id = ?", (absolute, attachment_id))
        self.close()
        target.mkdir(parents=True, exist_ok=True)
        for name in items:
            source = old / name
            if not source.exists():
                continue
            if (target / name).is_dir():
                (target / name).rmdir()
            shutil.move(str(source), str(target / name))
        for suffix in ("-wal", "-shm", "-journal"):
            Path(str(old / DB_FILENAME) + suffix).unlink(missing_ok=True)
        return replace(self.config, library_root=target)

    def add_reference(self, meta: PaperMetadata, collection_id: int | None = None) -> IngestResult:
        """Add an entry without PDF from fetched metadata (e.g. "Add by DOI"); a known DOI only links the folder."""
        candidate = ReferenceCandidate(key="", metadata=meta)
        return self.commit_reference(
            candidate, keep_key=False, folder_ids=[collection_id] if collection_id is not None else []
        )
