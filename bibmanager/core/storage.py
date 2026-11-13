"""Physical PDF storage: deterministic naming, collision handling, ``pdfs/<YYYY>/`` filing.

Invariant: every *managed* PDF lives exactly once at ``<library_root>/pdfs/<YYYY|Unknown>/<citation_key>.pdf``.
Project folders never hold files; they are rows in SQLite (see ``database.py``). *Linked* PDFs (e.g. a
library imported from Mendeley "in place") stay where they are and are never renamed, moved or deleted.

Citation keys capitalise every surname component (``Anand_2023``, ``Ciambella_Lancioni_2025``,
``Van_Der_Giessen_et_al_2020``); ``et_al`` and the collision suffixes stay lower-case. Keys are unique
case-insensitively, because BibTeX/biber and the Windows/macOS file systems ignore case.
"""

from __future__ import annotations

import hashlib
import itertools
import logging
import os
import re
import shutil
import string
import uuid
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path, PurePosixPath

from unidecode import unidecode

from .models import SUPPLEMENT_TAG, YEAR_UNKNOWN_LABEL, Author

log = logging.getLogger(__name__)

PDFS_DIRNAME = "pdfs"
UNKNOWN_YEAR_DIRNAME = YEAR_UNKNOWN_LABEL
PROVISIONAL_KEY_MAX_LEN = 64

_SEPARATORS = re.compile(r"[\s\-]+")
_NON_KEY_CHARS = re.compile(r"[^a-z0-9_]")
_MULTI_UNDERSCORE = re.compile(r"_+")
_NAME_WORDS = re.compile(r"[\s\-_]+")
_NON_ALNUM = re.compile(r"[^A-Za-z0-9]")
# Managed keys double as file names: ASCII letters, digits, '_' and '-'.
_VALID_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_\-]*")
# Keys of linked / file-less entries (e.g. kept from Mendeley) may also use ':', '.' and '+'.
_VALID_LOOSE_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_\-:.+]*")
_INVALID_KEY_CHARS = re.compile(r"[^A-Za-z0-9_\-]+")

ImportMode = str  # "move" | "copy"


class StorageError(RuntimeError):
    pass


# --------------------------------------------------------------------------- naming
def normalize_key_component(text: str) -> str:
    """Lower-case ASCII; spaces/hyphens -> ``_``; accents transliterated; punctuation dropped.

    >>> normalize_key_component("García-Márquez")
    'garcia_marquez'
    >>> normalize_key_component("van der Giessen")
    'van_der_giessen'
    >>> normalize_key_component("D'Annunzio")
    'dannunzio'
    """
    ascii_text = unidecode(text or "").lower()
    ascii_text = _SEPARATORS.sub("_", ascii_text)
    ascii_text = _NON_KEY_CHARS.sub("", ascii_text)
    return _MULTI_UNDERSCORE.sub("_", ascii_text).strip("_")


def surname_key_component(name: str) -> str:
    """ASCII surname with the first letter of every part capitalised.

    All-lower/all-upper parts are normalised (``"DE LUCA"`` -> ``De_Luca``); mixed-case parts keep
    their inner capitals (``"McDonald"`` -> ``McDonald``).

    >>> surname_key_component("García-Márquez")
    'Garcia_Marquez'
    >>> surname_key_component("van der Giessen")
    'Van_Der_Giessen'
    >>> surname_key_component("D'Annunzio")
    'DAnnunzio'
    """
    parts: list[str] = []
    for word in _NAME_WORDS.split(unidecode(name or "")):
        word = _NON_ALNUM.sub("", word)
        if not word:
            continue
        if word.islower() or word.isupper():
            word = word.lower()
        parts.append(word[0].upper() + word[1:])
    return "_".join(parts)


def base_citation_key(authors: Sequence[Author], year: int | None) -> str | None:
    """``Surname_year`` / ``Surname1_Surname2_year`` / ``Surname1_et_al_year``.

    Returns ``None`` when authors or year are missing (the paper then needs review).
    """
    if year is None or not authors:
        return None
    first = surname_key_component(authors[0].key_name) or "Anon"
    if len(authors) == 1:
        parts = [first]
    elif len(authors) == 2:
        parts = [first, surname_key_component(authors[1].key_name) or "Anon"]
    else:
        parts = [first, "et_al"]
    return "_".join([*parts, str(year)])


def provisional_key(filename: str) -> str:
    """Key used while metadata is unresolved: the (sanitised) original filename stem."""
    stem = PurePosixPath(filename.replace("\\", "/")).stem
    key = normalize_key_component(stem)[:PROVISIONAL_KEY_MAX_LEN].strip("_")
    return key or "unnamed"


def collision_suffixes() -> Iterator[str]:
    """``""``, ``a`` … ``z``, ``aa``, ``ab`` … (unbounded)."""
    yield ""
    for width in itertools.count(1):
        for combo in itertools.product(string.ascii_lowercase, repeat=width):
            yield "".join(combo)


def key_matches_base(key: str, base: str) -> bool:
    """True if ``key`` is ``base`` optionally followed by a collision suffix."""
    return re.fullmatch(re.escape(base) + r"[a-z]*", key) is not None


def validate_citation_key(key: str, *, filename: bool = True) -> str:
    """Check a user-supplied key. ``filename=False`` for linked / file-less entries (also ``: . +``)."""
    if filename and not _VALID_KEY.fullmatch(key or ""):
        raise ValueError(
            f"Invalid citation key {key!r}: use letters, digits, '_' or '-' (the key is also the PDF filename)."
        )
    if not filename and not _VALID_LOOSE_KEY.fullmatch(key or ""):
        raise ValueError(f"Invalid citation key {key!r}: use letters, digits and '_', '-', ':', '.', '+'.")
    return key


def sanitize_citation_key(key: str, *, filename: bool = True) -> str:
    """Best-effort repair of a foreign key (e.g. from Mendeley) into a valid one."""
    text = unidecode(key or "").strip()
    if filename:
        text = _INVALID_KEY_CHARS.sub("_", text)
    else:
        text = re.sub(r"[^A-Za-z0-9_\-:.+]+", "_", text)
    text = _MULTI_UNDERSCORE.sub("_", text).strip("_-:.+")
    return text or "unnamed"


def same_file(a: Path, b: Path | None) -> bool:
    """True if ``a`` and ``b`` are the same file (also across case-only differences on Windows/macOS)."""
    if b is None:
        return False
    try:
        return a.exists() and b.exists() and os.path.samefile(a, b)
    except OSError:
        return False


def sha256_file(path: Path, chunk_size: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as fh:
        for chunk in iter(lambda: fh.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


# --------------------------------------------------------------------------- storage
class PdfStorage:
    """Owns ``<library_root>/pdfs``. All paths stored in SQLite are POSIX and relative."""

    def __init__(self, library_root: Path) -> None:
        self.root = Path(library_root)
        self.pdfs_dir = self.root / PDFS_DIRNAME

    def ensure_layout(self) -> None:
        (self.pdfs_dir / UNKNOWN_YEAR_DIRNAME).mkdir(parents=True, exist_ok=True)

    @staticmethod
    def year_dirname(year: int | None) -> str:
        return str(year) if year is not None else UNKNOWN_YEAR_DIRNAME

    def relative_path_for(self, citation_key: str, year: int | None) -> str:
        return PurePosixPath(PDFS_DIRNAME, self.year_dirname(year), f"{citation_key}.pdf").as_posix()

    def absolute_path(self, relative_path: str) -> Path:
        return self.root.joinpath(*PurePosixPath(relative_path).parts)

    def allocate_citation_key(
        self,
        base: str,
        year: int | None,
        is_key_taken: Callable[[str], bool],
        *,
        own_relative_path: str | None = None,
    ) -> str:
        """First free ``base + suffix``: free in the DB *and* no foreign file on disk."""
        own = self.absolute_path(own_relative_path) if own_relative_path else None
        for suffix in collision_suffixes():
            candidate = f"{base}{suffix}"
            if is_key_taken(candidate):
                continue
            target = self.absolute_path(self.relative_path_for(candidate, year))
            if target.exists() and not same_file(target, own):
                continue
            return candidate
        raise AssertionError("unreachable")  # pragma: no cover

    def store(self, source: Path, citation_key: str, year: int | None, *, mode: ImportMode = "move") -> str:
        """Move (or copy) ``source`` into ``pdfs/<YYYY>/<key>.pdf``; return the relative path."""
        source = Path(source)
        if not source.is_file():
            raise StorageError(f"Source PDF not found: {source}")
        relative = self.relative_path_for(citation_key, year)
        dest = self.absolute_path(relative)
        if dest.exists():
            raise FileExistsError(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if mode == "copy":
            shutil.copy2(source, dest)
        elif mode == "move":
            shutil.move(str(source), str(dest))
        else:
            raise ValueError(f"Unknown import mode {mode!r}")
        log.info("Stored %s -> %s (%s)", source.name, relative, mode)
        return relative

    def supplement_path_for(self, citation_key: str, year: int | None, position: int, extension: str) -> str:
        """``pdfs/<YYYY>/<key>_S<n><ext>``: a supplementary file, next to the main PDF of its paper."""
        ext = extension.lower() if extension.startswith(".") or not extension else f".{extension.lower()}"
        name = f"{citation_key}{SUPPLEMENT_TAG}{position}{ext}"
        return PurePosixPath(PDFS_DIRNAME, self.year_dirname(year), name).as_posix()

    def store_as(self, source: Path, relative: str, *, mode: ImportMode = "copy") -> str:
        """Move (or copy) ``source`` to the library-relative path ``relative``; refuses to overwrite."""
        source = Path(source)
        if not source.is_file():
            raise StorageError(f"Source file not found: {source}")
        dest = self.absolute_path(relative)
        if dest.exists():
            raise FileExistsError(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if mode == "copy":
            shutil.copy2(source, dest)
        elif mode == "move":
            shutil.move(str(source), str(dest))
        else:
            raise ValueError(f"Unknown import mode {mode!r}")
        log.info("Stored %s -> %s (%s)", source.name, relative, mode)
        return relative

    def relocate(self, relative_path: str, new_key: str, new_year: int | None) -> str:
        """Rename/move an already-filed PDF (after an authors/year edit)."""
        return self.move_relative(relative_path, self.relative_path_for(new_key, new_year))

    def move_relative(self, relative_path: str, new_relative: str) -> str:
        """Rename/move a managed file inside the library (case-only renames included)."""
        if new_relative == relative_path:
            return relative_path
        src = self.absolute_path(relative_path)
        dest = self.absolute_path(new_relative)
        if not src.is_file():
            raise StorageError(f"Physical file missing: {src}")
        if dest.exists():
            if not same_file(src, dest):
                raise FileExistsError(dest)
            # Case-only rename (anand_2023 -> Anand_2023) on a case-insensitive file system:
            # go through a temporary name so the new spelling is actually written to disk.
            tmp = src.with_name(f".rename-{uuid.uuid4().hex}{src.suffix}")
            os.replace(src, tmp)
            if dest.exists() and same_file(tmp, dest):  # two hard links to one file: keep the wanted name only
                tmp.unlink()
            else:
                os.replace(tmp, dest)
        else:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dest))
        log.info("Relocated %s -> %s", relative_path, new_relative)
        return new_relative

    def delete(self, relative_path: str) -> bool:
        path = self.absolute_path(relative_path)
        if path.is_file():
            path.unlink()
            return True
        return False

    def all_pdfs(self) -> list[Path]:
        return sorted(self.pdfs_dir.rglob("*.pdf")) if self.pdfs_dir.exists() else []
