"""Search queries with field qualifiers (pure Python, used by the GUI filter and testable headless).

Syntax (terms are combined with AND; matching ignores case and accents)::

    anand fracture                 free words, searched in the selected scope (default: all fields)
    "phase field"                  exact phrase
    author:anand  au:"van der"     field qualifiers (see FIELDS for names and short aliases)
    journal:solids  in:"J. Mech"   publication (journal, proceedings, school...)
    year:2020  year:2015-2020  year:>=2018  year:<2000  year:2018-
    tag:review  kw:hydrogel  folder:thesis  type:book  key:anand_2023  doi:10.1016
    is:starred  is:unread  is:read  is:review  is:mine  is:opened
    has:pdf  has:notes  has:doi  has:supp  has:project
    -tag:review  -author:smith  -word   exclusion

A ``word:`` prefix that is not a known field (``http:``, ``10.1016/x:y``) is kept as plain text.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from unidecode import unidecode

from .models import Paper, split_list_field

# canonical field -> aliases accepted before ":"
FIELDS: dict[str, tuple[str, ...]] = {
    "author": ("author", "authors", "au", "a", "by"),
    "title": ("title", "ti", "t"),
    "journal": ("journal", "published", "publication", "in", "j", "source", "venue", "booktitle"),
    "year": ("year", "y", "yr"),
    "tag": ("tag", "tags", "mytag"),
    "keyword": ("keyword", "keywords", "kw"),
    "abstract": ("abstract", "abs"),
    "notes": ("notes", "note"),
    "key": ("key", "citekey", "citationkey", "cite"),
    "id": ("id", "doi", "arxiv", "isbn", "issn", "pmid"),
    "folder": ("folder", "folders", "collection", "project"),
    "type": ("type",),
    "publisher": ("publisher",),
    "file": ("file", "pdf"),
}
ALIASES = {alias: name for name, aliases in FIELDS.items() for alias in aliases}
FLAGS = {
    "is": {"favorite", "favourite", "fav", "starred", "read", "unread", "review", "mine", "linked", "opened"},
    "has": {"pdf", "file", "notes", "note", "doi", "abstract", "tags", "tag", "supp", "supplement", "supplementary", "si",
            "project", "folder"},
}
# Fields searched by free words in the "all fields" scope (full text is a separate scope, via FTS5).
ALL_FIELDS = ("author", "title", "journal", "year", "tag", "keyword", "abstract", "notes", "key", "id", "publisher")
SCOPES = ("all", *ALL_FIELDS[:-1], "folder")  # what the search box menu offers

_TOKEN = re.compile(r'(-?)(?:([A-Za-z_]+):)?("([^"]*)"?|\S+)')
_YEAR_RANGE = re.compile(r"^(\d{4})?\s*(?:-|\.\.)\s*(\d{4})?$")
_YEAR_CMP = re.compile(r"^(<=|>=|<|>|=)?\s*(\d{4})$")


def fold(text: str) -> str:
    """Case- and accent-insensitive form used on both sides of every comparison."""
    return unidecode(text or "").casefold()


@dataclass(frozen=True, slots=True)
class Term:
    field: str | None  # canonical field, "is"/"has" for flags, None = free text (scope)
    value: str  # folded text, flag name, or "" for year terms
    negate: bool = False
    year_min: int | None = None
    year_max: int | None = None


@dataclass(slots=True)
class Query:
    terms: list[Term] = field(default_factory=list)
    text: str = ""

    @property
    def free_text(self) -> str:
        """Free words (unfolded phrases re-quoted) -- what goes to FTS5 in the full-text scope."""
        parts = []
        for term in self.terms:
            if term.field is None and not term.negate:
                parts.append(f'"{term.value}"' if " " in term.value else term.value)
        return " ".join(parts)

    @property
    def field_terms(self) -> list[Term]:
        return [t for t in self.terms if t.field is not None or t.negate]

    def __bool__(self) -> bool:
        return bool(self.terms)


def _year_term(value: str, negate: bool) -> Term | None:
    value = value.strip()
    if m := _YEAR_RANGE.match(value):
        if not (m.group(1) or m.group(2)):
            return None
        low = int(m.group(1)) if m.group(1) else None
        high = int(m.group(2)) if m.group(2) else None
        return Term("year", "", negate, low, high)
    if m := _YEAR_CMP.match(value):
        op, year = m.group(1) or "=", int(m.group(2))
        low, high = {"=": (year, year), ">": (year + 1, None), ">=": (year, None), "<": (None, year - 1),
                     "<=": (None, year)}[op]  # fmt: skip
        return Term("year", "", negate, low, high)
    return None


def parse_query(text: str) -> Query:
    query = Query(text=text or "")
    for match in _TOKEN.finditer(text or ""):
        negate = match.group(1) == "-"
        prefix = (match.group(2) or "").lower()
        quoted = match.group(4)
        raw = quoted if quoted is not None else match.group(3)
        if prefix and prefix not in ALIASES and prefix not in FLAGS:  # e.g. "http://x" -> plain text
            raw, prefix = f"{match.group(2)}:{raw}", ""
        if not raw.strip() and not prefix:
            continue
        if prefix in FLAGS:
            flag = fold(raw).strip()
            if flag in FLAGS[prefix]:
                query.terms.append(Term(prefix, flag, negate))
            else:  # unknown flag: search the words instead of silently matching nothing
                query.terms.append(Term(None, fold(f"{prefix}:{raw}"), negate))
            continue
        name = ALIASES.get(prefix) if prefix else None
        if name == "year":
            term = _year_term(raw, negate)
            if term is not None:
                query.terms.append(term)
                continue
        value = fold(raw).strip()
        if value:
            query.terms.append(Term(name, value, negate))
    return query


# --------------------------------------------------------------------------- matching
@dataclass(slots=True)
class SearchRecord:
    """Folded searchable text per field for one paper (built once per list refresh)."""

    fields: dict[str, str]
    year: int | None
    flags: frozenset[str]
    all_text: str = ""


def build_record(paper: Paper, folders: Iterable[str] = ()) -> SearchRecord:
    folders = list(folders)
    m = paper.meta
    authors = "; ".join(
        f"{a.family}, {a.given}".rstrip(", ") + (f" {a.given} {a.family}" if a.family and a.given else "")
        if a.family
        else a.literal
        for a in m.authors
    )
    containers = [m.journal, *(m.extra_fields.get(k, "") for k in ("booktitle", "school", "institution", "series"))]
    fields = {
        "author": authors,
        "title": m.title,
        "journal": " ; ".join(c for c in containers if c),
        "year": m.year_label,
        "tag": paper.tags,
        "keyword": m.keywords,
        "abstract": m.abstract,
        "notes": paper.notes,
        "key": paper.citation_key,
        "id": " ".join((m.doi, m.arxiv_id, m.isbn, m.issn, m.pmid, m.url)),
        "folder": " ; ".join(folders),
        "type": m.entry_type,
        "publisher": m.publisher,
        "file": paper.relative_pdf_path or "",
    }
    folded = {name: fold(value) for name, value in fields.items()}
    flags = {
        "favorite" if paper.favorite else "",
        "read" if paper.is_read else "unread",
        "review" if paper.needs_review else "",
        "mine" if paper.my_publication else "",
        "linked" if paper.pdf_linked else "",
        "pdf" if paper.has_pdf else "",
        "notes" if paper.notes.strip() else "",
        "doi" if m.doi else "",
        "abstract" if m.abstract.strip() else "",
        "tags" if split_list_field(paper.tags) else "",
        "supp" if paper.attachments else "",
        "project" if any(folders) else "",
        "opened" if paper.last_opened_at else "",
    }
    flags.discard("")
    all_text = " \n".join(folded[name] for name in ALL_FIELDS)
    return SearchRecord(folded, m.year, frozenset(flags), all_text)


_FLAG_SYNONYMS = {"favourite": "favorite", "fav": "favorite", "starred": "favorite", "file": "pdf", "note": "notes",
                  "tag": "tags", "supplement": "supp", "supplementary": "supp", "si": "supp",
                  "folder": "project"}  # fmt: skip


def _term_matches(term: Term, record: SearchRecord, scope: str) -> bool:
    if term.field in ("is", "has"):
        return _FLAG_SYNONYMS.get(term.value, term.value) in record.flags
    if term.field == "year" and (term.year_min is not None or term.year_max is not None):
        if record.year is None:
            return False
        if term.year_min is not None and record.year < term.year_min:
            return False
        return term.year_max is None or record.year <= term.year_max
    if term.field is None:
        haystack = record.all_text if scope == "all" else record.fields.get(scope, "")
        return term.value in haystack
    return term.value in record.fields.get(term.field, "")


def matches(query: Query, record: SearchRecord, scope: str = "all", *, skip_free_text: bool = False) -> bool:
    """AND of all terms; ``skip_free_text`` when free words were already answered by full-text search."""
    for term in query.terms:
        if skip_free_text and term.field is None and not term.negate:
            continue
        if _term_matches(term, record, scope) == term.negate:
            return False
    return True


SCOPE_LABELS: Mapping[str, str] = {
    "all": "All fields",
    "author": "Authors",
    "title": "Title",
    "journal": "Venue",
    "year": "Year",
    "tag": "Tags",
    "keyword": "Author keywords",
    "abstract": "Abstract",
    "notes": "Notes",
    "key": "Citation key",
    "id": "Identifiers (DOI, arXiv, ISBN, PMID)",
    "folder": "Projects",
}

SYNTAX_HELP = (
    ("author:anand", "authors containing “anand” (also au:)"),
    ('author:"van der giessen"', "a phrase inside one field"),
    ("venue:solids", "journal, proceedings, school (also journal:, in:)"),
    ("year:2020, year:2015-2020, year:>=2018", "year or range"),
    ("title:wrinkling", "title"),
    ("tag:review, kw:hydrogel", "your tags, author keywords"),
    ("project:thesis, type:book, key:anand", "project, entry type, citation key"),
    ("doi:10.1016", "DOI, arXiv ID, ISBN, ISSN, PMID"),
    ("is:starred, is:unread, has:pdf", "flags (also is:mine, is:opened, has:notes, has:supp)"),
    ("-has:project", "in no project"),
    ("-tag:review, -smith", "exclude"),
)
