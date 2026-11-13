"""SQLite persistence: papers, logical collections (M:N) and the FTS5 full-text index.

The connection is thread-bound (``sqlite3`` default): use a ``Database`` only from the
thread that created it. Background workers must not touch it (see ``Library.prepare_import``).
"""

from __future__ import annotations

import hashlib
import logging
import re
import sqlite3
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .models import (
    Collection,
    Paper,
    PaperMetadata,
    SearchHit,
    authors_from_json,
    authors_to_json,
    extra_fields_from_json,
    extra_fields_to_json,
)

SCHEMA_VERSION = 4

# Schema v3 (rebuilt in place from v1/v2 by ``Database._migrate``):
# * ``sha256`` / ``relative_pdf_path`` are NULL for references without a PDF (e.g. imported from a .bib);
# * ``pdf_linked = 1``: the PDF is a *linked* file left where it was (absolute path, or POSIX path relative
#   to the library root) that JRef never renames, moves or deletes; ``0``: managed ``pdfs/<YYYY>/<key>.pdf``;
# * ``citation_key`` is unique case-insensitively (BibTeX and Windows/macOS file names ignore case);
# * ``extra_fields``: JSON ``{bibtex_field: raw LaTeX value}`` passed through verbatim (editor, address, ...).
# Schema v4: ``uid`` on papers and collections -- the identity of a row across the devices that share the library
# (``id`` is local to one database) -- and the ``sync_*`` bookkeeping tables of core/sync.py. Only the citation
# key is unique at the database level: the same PDF or DOI added on two devices before they synchronise must be
# storable as two entries (the import pipeline still refuses duplicates on one device).
_PAPERS_TABLE = """
CREATE TABLE IF NOT EXISTS {name} (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    sha256            TEXT,
    doi               TEXT,
    arxiv_id          TEXT,
    citation_key      TEXT    NOT NULL UNIQUE COLLATE NOCASE,
    entry_type        TEXT    NOT NULL DEFAULT 'article',
    title             TEXT    NOT NULL DEFAULT '',
    authors_json      TEXT    NOT NULL DEFAULT '[]',
    year              INTEGER,
    journal           TEXT,
    volume            TEXT,
    issue             TEXT,
    pages             TEXT,
    publisher         TEXT,
    abstract          TEXT,
    url               TEXT,
    isbn              TEXT,
    issn              TEXT,
    pmid              TEXT,
    keywords          TEXT,
    extra_fields      TEXT,
    relative_pdf_path TEXT,
    pdf_linked        INTEGER NOT NULL DEFAULT 0 CHECK (pdf_linked IN (0, 1)),
    needs_review      INTEGER NOT NULL DEFAULT 0 CHECK (needs_review IN (0, 1)),
    notes             TEXT,
    tags              TEXT,
    favorite          INTEGER NOT NULL DEFAULT 0,
    is_read           INTEGER NOT NULL DEFAULT 0,
    last_opened_at    TEXT,
    my_publication    INTEGER NOT NULL DEFAULT 0,
    created_at        TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    uid               TEXT
);
"""

_SCHEMA = (
    _PAPERS_TABLE.format(name="papers")
    + """
DROP INDEX IF EXISTS ux_papers_doi;
CREATE INDEX        IF NOT EXISTS ix_papers_doi   ON papers(doi)      WHERE doi IS NOT NULL;
CREATE INDEX        IF NOT EXISTS ix_papers_sha   ON papers(sha256)   WHERE sha256 IS NOT NULL;
CREATE INDEX        IF NOT EXISTS ix_papers_path  ON papers(relative_pdf_path) WHERE relative_pdf_path IS NOT NULL;
CREATE INDEX        IF NOT EXISTS ix_papers_arxiv ON papers(arxiv_id) WHERE arxiv_id IS NOT NULL;
CREATE INDEX        IF NOT EXISTS ix_papers_year  ON papers(year);
CREATE INDEX        IF NOT EXISTS ix_papers_created ON papers(created_at);

CREATE TABLE IF NOT EXISTS collections (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL,
    slug            TEXT NOT NULL UNIQUE,
    parent_id       INTEGER REFERENCES collections(id) ON DELETE CASCADE,
    bib_export_path TEXT,
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    uid             TEXT
);

CREATE TABLE IF NOT EXISTS paper_collections (
    paper_id      INTEGER NOT NULL REFERENCES papers(id)      ON DELETE CASCADE,
    collection_id INTEGER NOT NULL REFERENCES collections(id) ON DELETE CASCADE,
    added_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    PRIMARY KEY (paper_id, collection_id)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS ix_paper_collections_collection ON paper_collections(collection_id);

-- Device synchronisation (core/sync.py). All of it is local bookkeeping of this database.
CREATE TABLE IF NOT EXISTS sync_meta   (key TEXT PRIMARY KEY, value TEXT) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS sync_shadow (kind TEXT NOT NULL, uid TEXT NOT NULL, data TEXT NOT NULL,
                                        PRIMARY KEY (kind, uid)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS sync_clock  (key TEXT PRIMARY KEY, ts TEXT NOT NULL) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS sync_cursor (device TEXT NOT NULL, file TEXT NOT NULL, offset INTEGER NOT NULL,
                                        PRIMARY KEY (device, file)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS sync_pending (id INTEGER PRIMARY KEY AUTOINCREMENT, wait_uid TEXT NOT NULL, op TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS ix_sync_pending_uid ON sync_pending(wait_uid);
"""
)

_UID_INDEXES = """
CREATE UNIQUE INDEX IF NOT EXISTS ux_papers_uid      ON papers(uid)      WHERE uid IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS ux_collections_uid ON collections(uid) WHERE uid IS NOT NULL;
"""

_FTS_SCHEMA = (
    """
-- FTS5 rowid == papers.id (paper_id is kept as an explicit UNINDEXED column, per SPEC).
CREATE VIRTUAL TABLE IF NOT EXISTS papers_fts USING fts5(
    paper_id UNINDEXED,
    title,
    authors_text,
    abstract,
    full_text,
    tokenize = 'unicode61 remove_diacritics 2'
);

CREATE TRIGGER IF NOT EXISTS trg_papers_delete_fts AFTER DELETE ON papers BEGIN
    DELETE FROM papers_fts WHERE rowid = old.id;
END;
"""
)
_V3_MARKER_COLUMNS = ("pdf_linked", "extra_fields")
_TEXT_COLUMNS = (
    "doi", "arxiv_id", "journal", "volume", "issue", "pages", "publisher", "abstract",
    "url", "isbn", "issn", "pmid", "keywords",
)  # fmt: skip
# Library bookkeeping that never affects BibTeX output.
FLAG_COLUMNS = frozenset({"favorite", "is_read", "last_opened_at", "tags", "my_publication", "notes"})
_FTS_TOKEN = re.compile(r'[^\s"]+')

log = logging.getLogger(__name__)


class DatabaseError(RuntimeError):
    pass


def new_uid() -> str:
    return uuid.uuid4().hex


def collection_uid(slug: str) -> str:
    """Identity of a folder created under ``slug``: the same on every device, so a folder created with the
    same name on two devices before they synchronise is one folder afterwards."""
    return hashlib.sha1(f"jref-collection:{slug}".encode()).hexdigest()[:32]


def _none_if_empty(value: str | None) -> str | None:
    value = (value or "").strip()
    return value or None


def _meta_columns(meta: PaperMetadata) -> dict[str, Any]:
    columns: dict[str, Any] = {
        "entry_type": meta.entry_type or "article",
        "title": meta.title or "",
        "authors_json": authors_to_json(meta.authors),
        "year": meta.year,
    }
    for name in _TEXT_COLUMNS:
        columns[name] = _none_if_empty(getattr(meta, name))
    if columns["doi"]:
        columns["doi"] = columns["doi"].lower()
    columns["extra_fields"] = extra_fields_to_json(meta.extra_fields)
    return columns


def _rowid(cursor: sqlite3.Cursor) -> int:
    if cursor.lastrowid is None:  # pragma: no cover - sqlite always sets it after INSERT
        raise DatabaseError("INSERT did not return a rowid")
    return cursor.lastrowid


def to_fts_query(user_query: str) -> str:
    """Turn free text into a safe FTS5 query: every token quoted, implicit AND, ``tok*`` prefix kept."""
    terms = []
    for token in _FTS_TOKEN.findall(user_query or ""):
        prefix = token.endswith("*")
        token = token.rstrip("*")
        if token:
            terms.append(f'"{token}"' + ("*" if prefix else ""))
    return " ".join(terms)


class Database:
    def __init__(self, path: str | Path) -> None:
        self.path = path
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        # isolation_level=None -> autocommit; transactions are explicit (see ``transaction``).
        self.conn = sqlite3.connect(str(path), isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self._tx_depth = 0
        self.has_fts = True
        self._migrate()

    # ------------------------------------------------------------------ infrastructure
    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> Database:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _migrate(self) -> None:
        version = self.conn.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise DatabaseError(f"library.db schema v{version} is newer than this program (v{SCHEMA_VERSION})")
        self.conn.executescript(_SCHEMA)
        info = {row["name"]: row for row in self.conn.execute("PRAGMA table_info(papers)")}
        if any(c not in info for c in (*_V3_MARKER_COLUMNS, "uid")) or info["sha256"]["notnull"]:
            self._backup_before_upgrade(version)
            self._rebuild_papers_table(list(info))  # also drops the UNIQUE constraints of v3 (see above)
            self.conn.executescript(_SCHEMA)  # indexes went away with the old table
        self._ensure_uids()
        self._ensure_fts()
        self.conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def _backup_before_upgrade(self, version: int) -> None:
        """Keep a copy of a non-empty database of an earlier release next to it before rebuilding its main
        table (``library.db.v3.bak``): an older JRef/JBibManager can still open the copy."""
        if str(self.path) == ":memory:" or not self.conn.execute("SELECT 1 FROM papers LIMIT 1").fetchone():
            return
        target = Path(f"{self.path}.v{version}.bak")
        if target.exists():
            return
        try:
            backup = sqlite3.connect(str(target))
            try:
                self.conn.backup(backup)
            finally:
                backup.close()
            log.info("Saved %s before upgrading the database", target)
        except (OSError, sqlite3.Error) as exc:  # pragma: no cover - e.g. read-only folder: upgrade anyway
            log.warning("Could not save a backup copy before the upgrade: %s", exc)

    def _ensure_uids(self) -> None:
        """v3 -> v4: add the ``uid`` columns and give every existing row its identity."""
        for table in ("papers", "collections"):
            columns = {row["name"] for row in self.conn.execute(f"PRAGMA table_info({table})")}
            if "uid" not in columns:
                self.conn.execute(f"ALTER TABLE {table} ADD COLUMN uid TEXT")
        self.conn.executescript(_UID_INDEXES)
        with self.transaction():
            self.conn.execute("UPDATE papers SET uid = lower(hex(randomblob(16))) WHERE uid IS NULL")
            taken = {r[0] for r in self.conn.execute("SELECT uid FROM collections WHERE uid IS NOT NULL")}
            for cid, slug in self.conn.execute("SELECT id, slug FROM collections WHERE uid IS NULL").fetchall():
                uid = collection_uid(slug)
                uid = new_uid() if uid in taken else uid
                taken.add(uid)
                self.conn.execute("UPDATE collections SET uid = ? WHERE id = ?", (uid, cid))

    def _ensure_fts(self) -> None:
        """Create the full-text index. A SQLite built without FTS5 (some Android builds) still gives a working
        library: ``has_fts`` is False and full-text search falls back to a substring scan of the metadata."""
        try:
            self.conn.executescript(_FTS_SCHEMA)
            self.has_fts = True
        except sqlite3.OperationalError as exc:
            log.warning("FTS5 is not available (%s): full-text search is limited to metadata", exc)
            self.has_fts = False

    def _rebuild_papers_table(self, old_columns: list[str]) -> None:
        """v1/v2 -> v3: SQLite cannot relax NOT NULL in place, so copy into a new table (ids preserved,
        hence FTS rowids and folder links stay valid). Follows the 12-step recipe of the SQLite manual."""
        log.info("Upgrading library.db to schema v%s", SCHEMA_VERSION)
        self.conn.execute("PRAGMA foreign_keys = OFF")
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                self.conn.execute("DROP TABLE IF EXISTS papers_v3")
                self.conn.execute("DROP TRIGGER IF EXISTS trg_papers_delete_fts")  # recreated by _ensure_fts
                self.conn.execute(_PAPERS_TABLE.format(name="papers_v3"))
                target = {row["name"] for row in self.conn.execute("PRAGMA table_info(papers_v3)")}
                common = ", ".join(c for c in old_columns if c in target)
                self.conn.execute(f"INSERT INTO papers_v3 ({common}) SELECT {common} FROM papers")
                self.conn.execute("DROP TABLE papers")
                self.conn.execute("ALTER TABLE papers_v3 RENAME TO papers")
                broken = self.conn.execute("PRAGMA foreign_key_check").fetchall()
                if broken:
                    raise DatabaseError(f"Foreign key check failed after migration: {len(broken)} rows")
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise
            self.conn.execute("COMMIT")
        finally:
            self.conn.execute("PRAGMA foreign_keys = ON")

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Re-entrant transaction (outermost BEGIN IMMEDIATE, inner SAVEPOINTs)."""
        depth = self._tx_depth
        savepoint = f"sp_{depth}"
        self.conn.execute("BEGIN IMMEDIATE" if depth == 0 else f"SAVEPOINT {savepoint}")
        self._tx_depth += 1
        try:
            yield self.conn
        except BaseException:
            self._tx_depth -= 1
            if depth == 0:
                self.conn.execute("ROLLBACK")
            else:
                self.conn.execute(f"ROLLBACK TO {savepoint}")
                self.conn.execute(f"RELEASE {savepoint}")
            raise
        else:
            self._tx_depth -= 1
            self.conn.execute("COMMIT" if depth == 0 else f"RELEASE {savepoint}")

    # ------------------------------------------------------------------ papers
    @staticmethod
    def _row_to_paper(row: sqlite3.Row) -> Paper:
        meta = PaperMetadata(
            title=row["title"] or "",
            authors=authors_from_json(row["authors_json"]),
            year=row["year"],
            journal=row["journal"] or "",
            volume=row["volume"] or "",
            issue=row["issue"] or "",
            pages=row["pages"] or "",
            publisher=row["publisher"] or "",
            doi=row["doi"] or "",
            arxiv_id=row["arxiv_id"] or "",
            entry_type=row["entry_type"] or "article",
            abstract=row["abstract"] or "",
            url=row["url"] or "",
            isbn=row["isbn"] or "",
            issn=row["issn"] or "",
            pmid=row["pmid"] or "",
            keywords=row["keywords"] or "",
            extra_fields=extra_fields_from_json(row["extra_fields"]),
        )
        return Paper(
            id=row["id"],
            sha256=row["sha256"],
            citation_key=row["citation_key"],
            relative_pdf_path=row["relative_pdf_path"],
            meta=meta,
            pdf_linked=bool(row["pdf_linked"]),
            needs_review=bool(row["needs_review"]),
            notes=row["notes"] or "",
            created_at=row["created_at"],
            favorite=bool(row["favorite"]),
            is_read=bool(row["is_read"]),
            last_opened_at=row["last_opened_at"] or "",
            tags=row["tags"] or "",
            my_publication=bool(row["my_publication"]),
        )

    def insert_paper(
        self,
        *,
        sha256: str | None,
        citation_key: str,
        relative_pdf_path: str | None,
        meta: PaperMetadata,
        needs_review: bool = False,
        notes: str = "",
        pdf_linked: bool = False,
        uid: str | None = None,
    ) -> int:
        columns = _meta_columns(meta) | {
            "uid": uid or new_uid(),
            "sha256": sha256,
            "citation_key": citation_key,
            "relative_pdf_path": relative_pdf_path,
            "pdf_linked": int(pdf_linked and relative_pdf_path is not None),
            "needs_review": int(needs_review),
            "notes": _none_if_empty(notes),
        }
        names = ", ".join(columns)
        marks = ", ".join(f":{k}" for k in columns)
        with self.transaction():
            cur = self.conn.execute(f"INSERT INTO papers ({names}) VALUES ({marks})", columns)
        return _rowid(cur)

    def update_paper(
        self,
        paper_id: int,
        *,
        meta: PaperMetadata | None = None,
        citation_key: str | None = None,
        relative_pdf_path: str | None = None,
        needs_review: bool | None = None,
        pdf_linked: bool | None = None,
        sha256: str | None = None,
        **flags: Any,
    ) -> None:
        """Update bibliographic ``meta`` and/or bookkeeping columns (``FLAG_COLUMNS``)."""
        unknown = set(flags) - FLAG_COLUMNS
        if unknown:
            raise ValueError(f"Unknown paper columns: {sorted(unknown)}")
        columns: dict[str, Any] = _meta_columns(meta) if meta is not None else {}
        if citation_key is not None:
            columns["citation_key"] = citation_key
        if relative_pdf_path is not None:
            columns["relative_pdf_path"] = relative_pdf_path
        if needs_review is not None:
            columns["needs_review"] = int(needs_review)
        if pdf_linked is not None:
            columns["pdf_linked"] = int(pdf_linked)
        if sha256 is not None:
            columns["sha256"] = sha256
        for name, value in flags.items():
            if value is None:
                continue
            if name in {"favorite", "is_read", "my_publication"}:
                columns[name] = int(bool(value))
            else:
                columns[name] = _none_if_empty(str(value))
        if not columns:
            return
        assignments = ", ".join(f"{k} = :{k}" for k in columns)
        with self.transaction():
            cur = self.conn.execute(f"UPDATE papers SET {assignments} WHERE id = :_id", columns | {"_id": paper_id})
            if cur.rowcount != 1:
                raise KeyError(paper_id)
            if meta is not None:
                self._update_fts_metadata(paper_id, meta)

    def delete_paper(self, paper_id: int) -> None:
        with self.transaction():
            self.conn.execute("DELETE FROM papers WHERE id = ?", (paper_id,))

    def _fetch_paper(self, where: str, params: Sequence[Any]) -> Paper | None:
        row = self.conn.execute(f"SELECT * FROM papers WHERE {where}", params).fetchone()
        return self._row_to_paper(row) if row else None

    def get_paper(self, paper_id: int) -> Paper | None:
        return self._fetch_paper("id = ?", (paper_id,))

    def get_paper_by_sha256(self, sha256: str) -> Paper | None:
        return self._fetch_paper("sha256 = ?", (sha256,))

    def get_paper_by_doi(self, doi: str) -> Paper | None:
        return self._fetch_paper("doi = ?", ((doi or "").strip().lower(),))

    def get_paper_by_citation_key(self, key: str) -> Paper | None:
        """Case-insensitive (the column is ``COLLATE NOCASE``)."""
        return self._fetch_paper("citation_key = ?", (key,))

    def citation_key_exists(self, key: str, *, exclude_id: int | None = None) -> bool:
        """Case-insensitive: ``Anand_2023`` and ``anand_2023`` cannot coexist."""
        row = self.conn.execute(
            "SELECT 1 FROM papers WHERE citation_key = ? AND id IS NOT ?", (key, exclude_id)
        ).fetchone()
        return row is not None

    def get_paper_by_pdf_path(self, relative_pdf_path: str) -> Paper | None:
        return self._fetch_paper("relative_pdf_path = ?", (relative_pdf_path,))

    def _papers(self, sql: str, params: Sequence[Any] | dict[str, Any] = ()) -> list[Paper]:
        return [self._row_to_paper(r) for r in self.conn.execute(sql, params)]

    def list_papers(self) -> list[Paper]:
        return self._papers("SELECT * FROM papers ORDER BY citation_key")

    def papers_in_collection(self, collection_id: int) -> list[Paper]:
        return self._papers(
            "SELECT p.* FROM papers p JOIN paper_collections pc ON pc.paper_id = p.id "
            "WHERE pc.collection_id = ? ORDER BY p.citation_key",
            (collection_id,),
        )

    def papers_needing_review(self) -> list[Paper]:
        return self._papers("SELECT * FROM papers WHERE needs_review = 1 ORDER BY citation_key")

    def favorite_papers(self) -> list[Paper]:
        return self._papers("SELECT * FROM papers WHERE favorite = 1 ORDER BY citation_key")

    def my_publications(self) -> list[Paper]:
        return self._papers("SELECT * FROM papers WHERE my_publication = 1 ORDER BY citation_key")

    def recently_added(self, limit: int = 50) -> list[Paper]:
        return self._papers("SELECT * FROM papers ORDER BY created_at DESC, id DESC LIMIT ?", (limit,))

    def recently_read(self, limit: int = 50) -> list[Paper]:
        return self._papers(
            "SELECT * FROM papers WHERE last_opened_at IS NOT NULL ORDER BY last_opened_at DESC LIMIT ?", (limit,)
        )

    def unassigned_papers(self) -> list[Paper]:
        return self._papers(
            "SELECT * FROM papers WHERE id NOT IN (SELECT paper_id FROM paper_collections) ORDER BY citation_key"
        )

    def counts(self) -> dict[str, int]:
        """Document counts per view and per folder (``"folder:<id>"``), for the navigation pane."""
        row = self.conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(favorite), 0), COALESCE(SUM(needs_review), 0), "
            "COALESCE(SUM(my_publication), 0), "
            "COALESCE(SUM(id NOT IN (SELECT paper_id FROM paper_collections)), 0) FROM papers"
        ).fetchone()
        counts = dict(zip(("all", "favorites", "review", "mine", "unsorted"), (int(x) for x in row), strict=True))
        for cid, n in self.conn.execute("SELECT collection_id, COUNT(*) FROM paper_collections GROUP BY collection_id"):
            counts[f"folder:{cid}"] = int(n)
        return counts

    def count_papers(self) -> int:
        return int(self.conn.execute("SELECT COUNT(*) FROM papers").fetchone()[0])

    def search_metadata(self, text: str) -> list[Paper]:
        """Case-insensitive substring filter over key, title, authors, journal, DOI, keywords and tags."""
        escaped = (text or "").replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        like = f"%{escaped}%"
        return self._papers(
            "SELECT * FROM papers WHERE "
            + " OR ".join(
                f"{c} LIKE :q ESCAPE '\\'"
                for c in ("citation_key", "title", "authors_json", "journal", "doi", "keywords", "tags")
            )
            + " ORDER BY citation_key",
            {"q": like},
        )

    # ------------------------------------------------------------------ collections
    @staticmethod
    def _row_to_collection(row: sqlite3.Row) -> Collection:
        return Collection(
            id=row["id"],
            name=row["name"],
            slug=row["slug"],
            parent_id=row["parent_id"],
            bib_export_path=row["bib_export_path"],
        )

    def create_collection(
        self,
        name: str,
        slug: str,
        *,
        parent_id: int | None = None,
        bib_export_path: str | None = None,
        uid: str | None = None,
    ) -> Collection:
        if uid is None:  # deterministic unless that identity is in use or was deleted (then a fresh one)
            uid = collection_uid(slug)
            used = self.conn.execute(
                "SELECT 1 FROM collections WHERE uid = :u UNION ALL SELECT 1 FROM sync_clock WHERE key = 'cd:' || :u",
                {"u": uid},
            ).fetchone()
            if used is not None:
                uid = new_uid()
        with self.transaction():
            cur = self.conn.execute(
                "INSERT INTO collections (name, slug, parent_id, bib_export_path, uid) VALUES (?, ?, ?, ?, ?)",
                (name, slug, parent_id, bib_export_path, uid),
            )
        collection = self.get_collection(_rowid(cur))
        assert collection is not None
        return collection

    def update_collection(self, collection_id: int, **fields: Any) -> None:
        allowed = {"name", "slug", "parent_id", "bib_export_path"}
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"Unknown collection fields: {sorted(unknown)}")
        if not fields:
            return
        assignments = ", ".join(f"{k} = :{k}" for k in fields)
        with self.transaction():
            cur = self.conn.execute(
                f"UPDATE collections SET {assignments} WHERE id = :_id", fields | {"_id": collection_id}
            )
            if cur.rowcount != 1:
                raise KeyError(collection_id)

    def delete_collection(self, collection_id: int) -> None:
        with self.transaction():
            self.conn.execute("DELETE FROM collections WHERE id = ?", (collection_id,))

    def get_collection(self, collection_id: int) -> Collection | None:
        row = self.conn.execute("SELECT * FROM collections WHERE id = ?", (collection_id,)).fetchone()
        return self._row_to_collection(row) if row else None

    def get_collection_by_slug(self, slug: str) -> Collection | None:
        row = self.conn.execute("SELECT * FROM collections WHERE slug = ?", (slug,)).fetchone()
        return self._row_to_collection(row) if row else None

    def list_collections(self) -> list[Collection]:
        rows = self.conn.execute("SELECT * FROM collections ORDER BY name COLLATE NOCASE, id")
        return [self._row_to_collection(r) for r in rows]

    def slug_exists(self, slug: str, *, exclude_id: int | None = None) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM collections WHERE slug = ? AND id IS NOT ?", (slug, exclude_id)
        ).fetchone()
        return row is not None

    def descendant_collection_ids(self, collection_id: int) -> list[int]:
        """``collection_id`` and all its nested sub-collections."""
        rows = self.conn.execute(
            "WITH RECURSIVE tree(id) AS (SELECT ? UNION ALL "
            "SELECT c.id FROM collections c JOIN tree t ON c.parent_id = t.id) SELECT id FROM tree",
            (collection_id,),
        )
        return [int(r[0]) for r in rows]

    # ------------------------------------------------------------------ M:N membership
    def add_paper_to_collection(self, paper_id: int, collection_id: int) -> bool:
        """Return True if the link was created, False if it already existed."""
        with self.transaction():
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO paper_collections (paper_id, collection_id) VALUES (?, ?)",
                (paper_id, collection_id),
            )
        return cur.rowcount == 1

    def remove_paper_from_collection(self, paper_id: int, collection_id: int) -> bool:
        with self.transaction():
            cur = self.conn.execute(
                "DELETE FROM paper_collections WHERE paper_id = ? AND collection_id = ?", (paper_id, collection_id)
            )
        return cur.rowcount == 1

    def collection_names_by_paper(self) -> dict[int, list[str]]:
        """``paper_id -> [folder names]`` in one query (table "Folders" column)."""
        mapping: dict[int, list[str]] = {}
        rows = self.conn.execute(
            "SELECT pc.paper_id, c.name FROM paper_collections pc JOIN collections c ON c.id = pc.collection_id "
            "ORDER BY c.name COLLATE NOCASE"
        )
        for row in rows:
            mapping.setdefault(int(row[0]), []).append(row[1])
        return mapping

    def collections_of_paper(self, paper_id: int) -> list[Collection]:
        rows = self.conn.execute(
            "SELECT c.* FROM collections c JOIN paper_collections pc ON pc.collection_id = c.id "
            "WHERE pc.paper_id = ? ORDER BY c.name COLLATE NOCASE",
            (paper_id,),
        )
        return [self._row_to_collection(r) for r in rows]

    # ------------------------------------------------------------------ full text (FTS5)
    def index_fulltext(self, paper_id: int, meta: PaperMetadata, full_text: str) -> None:
        if not self.has_fts:
            return
        with self.transaction():
            self.conn.execute("DELETE FROM papers_fts WHERE rowid = ?", (paper_id,))
            self.conn.execute(
                "INSERT INTO papers_fts (rowid, paper_id, title, authors_text, abstract, full_text) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (paper_id, paper_id, meta.title, meta.authors_text(), meta.abstract, full_text),
            )

    def _update_fts_metadata(self, paper_id: int, meta: PaperMetadata) -> None:
        if not self.has_fts:
            return
        cur = self.conn.execute(
            "UPDATE papers_fts SET title = ?, authors_text = ?, abstract = ? WHERE rowid = ?",
            (meta.title, meta.authors_text(), meta.abstract, paper_id),
        )
        if cur.rowcount == 0:
            self.conn.execute(
                "INSERT INTO papers_fts (rowid, paper_id, title, authors_text, abstract, full_text) "
                "VALUES (?, ?, ?, ?, ?, '')",
                (paper_id, paper_id, meta.title, meta.authors_text(), meta.abstract),
            )

    def search_fulltext(self, query: str, *, limit: int = 200, raw: bool = False) -> list[SearchHit]:
        """BM25-ranked search (title > authors > abstract > body). ``raw`` passes FTS5 syntax through."""
        match = query if raw else to_fts_query(query)
        if not match:
            return []
        if not self.has_fts:
            return [SearchHit(paper_id=p.id, score=0.0, snippet="") for p in self.search_metadata(query)][:limit]
        rows = self.conn.execute(
            "SELECT rowid, bm25(papers_fts, 0.0, 10.0, 5.0, 3.0, 1.0) AS score, "
            "snippet(papers_fts, -1, '[', ']', ' ... ', 12) AS snip "
            "FROM papers_fts WHERE papers_fts MATCH ? ORDER BY score LIMIT ?",
            (match, limit),
        )
        return [SearchHit(paper_id=int(r["rowid"]), score=float(r["score"]), snippet=r["snip"]) for r in rows]
