"""Plain data model shared by every ``core`` module (no I/O, no Qt)."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

YEAR_UNKNOWN_LABEL = "Unknown"
MIN_YEAR = 1000
MAX_YEAR = 2999

# Lower-case nobiliary/toponymic particles that belong to the family name
# ("Erik van der Giessen" -> family "van der Giessen").
_NAME_PARTICLES = frozenset(
    {
        "van",
        "von",
        "der",
        "den",
        "de",
        "del",
        "della",
        "delle",
        "dei",
        "degli",
        "di",
        "da",
        "dal",
        "du",
        "des",
        "la",
        "le",
        "lo",
        "dos",
        "das",
        "do",
        "ten",
        "ter",
        "af",
        "av",
        "zu",
        "zum",
        "zur",
        "bin",
        "ibn",
        "st",
        "st.",
    }
)
_NAME_SUFFIXES = frozenset({"jr", "jr.", "sr", "sr.", "ii", "iii", "iv"})
_AUTHOR_LIST_SPLIT = re.compile(r"\s*;\s*|\s+and\s+", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class Author:
    """A single author. ``literal`` holds corporate names (e.g. a collaboration)."""

    family: str = ""
    given: str = ""
    suffix: str = ""
    literal: str = ""

    @property
    def key_name(self) -> str:
        """Name component used for citation keys (family name, else literal)."""
        return self.family or self.literal

    def display(self) -> str:
        if not self.family:
            return self.literal
        name = f"{self.given} {self.family}".strip()
        return f"{name}, {self.suffix}" if self.suffix else name

    def to_bibtex(self) -> str:
        """``Family, Suffix, Given`` (BibTeX name format); corporate names are braced."""
        if not self.family:
            return "{" + self.literal + "}" if self.literal else ""
        parts = [self.family]
        if self.suffix:
            parts.append(self.suffix)
        if self.given:
            parts.append(self.given)
        return ", ".join(parts)

    def to_dict(self) -> dict[str, str]:
        return {
            k: v
            for k, v in (
                ("family", self.family),
                ("given", self.given),
                ("suffix", self.suffix),
                ("literal", self.literal),
            )
            if v
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Author:
        return cls(
            family=str(data.get("family") or "").strip(),
            given=str(data.get("given") or "").strip(),
            suffix=str(data.get("suffix") or "").strip(),
            literal=str(data.get("literal") or "").strip(),
        )

    @classmethod
    def from_full_name(cls, name: str) -> Author:
        """Heuristic split of a free-form name ("Given Family" or "Family, Given")."""
        name = " ".join((name or "").split())
        if not name:
            return cls()
        if "," in name:
            family, _, given = name.partition(",")
            return cls(family=family.strip(), given=given.strip())
        tokens = name.split(" ")
        suffix = ""
        if len(tokens) > 2 and tokens[-1].lower() in _NAME_SUFFIXES:
            suffix = tokens.pop()
        if len(tokens) == 1:
            return cls(family=tokens[0], suffix=suffix)
        i = len(tokens) - 1
        while i > 1 and tokens[i - 1].lower() in _NAME_PARTICLES:
            i -= 1
        return cls(family=" ".join(tokens[i:]), given=" ".join(tokens[:i]), suffix=suffix)


def parse_author_list(text: str) -> list[Author]:
    """Parse ``"Anand, Lallit; van der Giessen, Erik"`` or BibTeX-style ``A and B``."""
    return [Author.from_full_name(chunk) for chunk in _AUTHOR_LIST_SPLIT.split(text or "") if chunk.strip()]


def split_list_field(text: str) -> list[str]:
    """Split a ``"; "``-separated field (tags, keywords) into trimmed, de-duplicated items."""
    return list(dict.fromkeys(item.strip() for item in re.split(r"[;\n]", text or "") if item.strip()))


def coerce_authors(value: Iterable[Author | Mapping[str, Any] | str] | str | None) -> list[Author]:
    if value is None:
        return []
    if isinstance(value, str):
        return parse_author_list(value)
    authors: list[Author] = []
    for item in value:
        if isinstance(item, Author):
            authors.append(item)
        elif isinstance(item, Mapping):
            authors.append(Author.from_dict(item))
        else:
            authors.append(Author.from_full_name(str(item)))
    return [a for a in authors if a.key_name]


def coerce_year(value: int | str | None) -> int | None:
    """Return a validated year, ``None`` for unknown; raise ``ValueError`` otherwise."""
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        if not text or text.lower() == YEAR_UNKNOWN_LABEL.lower():
            return None
        if not text.isdigit():
            raise ValueError(f"Invalid year: {value!r}")
        value = int(text)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"Invalid year: {value!r}")
    if not MIN_YEAR <= value <= MAX_YEAR:
        raise ValueError(f"Year out of range: {value}")
    return value


def authors_to_json(authors: Iterable[Author]) -> str:
    return json.dumps([a.to_dict() for a in authors], ensure_ascii=False)


def authors_from_json(text: str | None) -> list[Author]:
    if not text:
        return []
    return [Author.from_dict(d) for d in json.loads(text)]


def extra_fields_to_json(fields: Mapping[str, str]) -> str | None:
    clean = {str(k).lower(): str(v) for k, v in (fields or {}).items() if str(v or "").strip()}
    return json.dumps(dict(sorted(clean.items())), ensure_ascii=False) if clean else None


def extra_fields_from_json(text: str | None) -> dict[str, str]:
    if not text:
        return {}
    data = json.loads(text)
    return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}


@dataclass(slots=True)
class PaperMetadata:
    """Bibliographic metadata, independent of library bookkeeping."""

    title: str = ""
    authors: list[Author] = field(default_factory=list)
    year: int | None = None
    journal: str = ""
    volume: str = ""
    issue: str = ""
    pages: str = ""
    publisher: str = ""
    doi: str = ""
    arxiv_id: str = ""
    entry_type: str = "article"
    abstract: str = ""
    url: str = ""
    isbn: str = ""
    issn: str = ""
    pmid: str = ""
    keywords: str = ""  # author keywords, "; "-separated
    # Further BibTeX fields kept verbatim (raw LaTeX), e.g. editor/address/edition from an imported .bib.
    extra_fields: dict[str, str] = field(default_factory=dict)

    @property
    def year_label(self) -> str:
        return str(self.year) if self.year is not None else YEAR_UNKNOWN_LABEL

    def authors_text(self) -> str:
        return "; ".join(a.display() for a in self.authors)

    def authors_inverted(self) -> str:
        """``Family, Given; Family, Given`` (reference-manager list style, round-trips via ``parse_author_list``)."""
        return "; ".join(f"{a.family}, {a.given}".rstrip(", ") if a.family else a.literal for a in self.authors)

    def is_complete_for_naming(self) -> bool:
        return bool(self.authors) and self.year is not None


@dataclass(slots=True)
class Paper:
    """A library entry: at most one PDF, either managed (``pdfs/<YYYY>/<key>.pdf``) or linked in place.

    ``relative_pdf_path``/``sha256`` are ``None`` for references without a PDF.
    """

    id: int
    sha256: str | None
    citation_key: str
    relative_pdf_path: str | None
    meta: PaperMetadata
    pdf_linked: bool = False
    needs_review: bool = False
    notes: str = ""
    created_at: str = ""
    favorite: bool = False
    is_read: bool = False
    last_opened_at: str = ""
    tags: str = ""  # user tags, "; "-separated
    my_publication: bool = False

    @property
    def has_pdf(self) -> bool:
        return self.relative_pdf_path is not None

    @property
    def pdf_managed(self) -> bool:
        """True if bibmanager owns the file (renames it with the key, files it by year, deletes it)."""
        return self.relative_pdf_path is not None and not self.pdf_linked


@dataclass(slots=True)
class Collection:
    """A logical project folder. ``bib_export_path`` is the optional external target."""

    id: int
    name: str
    slug: str
    parent_id: int | None = None
    bib_export_path: str | None = None


@dataclass(frozen=True, slots=True)
class SearchHit:
    paper_id: int
    score: float
    snippet: str
