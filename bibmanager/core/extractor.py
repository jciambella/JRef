"""Metadata extraction pipeline (pure Python, no Qt).

    PDF (PyMuPDF: info dict, XMP, first two pages, largest-font lines)
      -> DOI      -> Crossref
      -> arXiv ID -> arXiv API (-> Crossref when the preprint links a published DOI)
      -> title    -> OpenAlex search (fuzzy title match; -> Crossref when a DOI is returned)
      -> nothing  -> ``needs_review``

Identifiers found on page 1 / XMP / info dict are trusted; identifiers found only on page 2
(often citations in the reference list) are accepted only if the returned title is actually
printed on page 1. Everything here is blocking: in the GUI it must run in a worker.
"""

from __future__ import annotations

import difflib
import html
import logging
import re
import xml.etree.ElementTree as ET
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
from unidecode import unidecode

try:  # PyMuPDF: desktop back end (text with font sizes, XMP, outline)
    import pymupdf
except ImportError:  # e.g. Android, where no PyMuPDF build exists: pure-Python pypdf is used instead
    pymupdf = None  # type: ignore[assignment]

from .models import Author, PaperMetadata, coerce_year

log = logging.getLogger(__name__)

DOI_PATTERN = re.compile(r"10\.\d{4,9}/[-._;()/:A-Za-z0-9]+")
_ARXIV_PATTERNS = (
    re.compile(r"arxiv\s*:\s*(\d{4}\.\d{4,5})(?:v\d+)?", re.IGNORECASE),
    re.compile(r"arxiv\.org/(?:abs|pdf)/(\d{4}\.\d{4,5})(?:v\d+)?", re.IGNORECASE),
    re.compile(r"arxiv\s*:\s*([a-z][a-z\-]*(?:\.[a-z]{2})?/\d{7})(?:v\d+)?", re.IGNORECASE),
    re.compile(r"arxiv\.org/(?:abs|pdf)/([a-z][a-z\-]*(?:\.[a-z]{2})?/\d{7})(?:v\d+)?", re.IGNORECASE),
)
_ARXIV_FILENAME = re.compile(r"(\d{4}\.\d{4,5})(?:v\d+)?")
_DOI_PREFIX = re.compile(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", re.IGNORECASE)
_NON_ALNUM = re.compile(r"[^a-z0-9]+")
_BLOCK_TAG = re.compile(r"</?(?:[a-z]+:)?(?:p|title|sec|br|div|li|list|list-item|abstract)\b[^>]*>", re.IGNORECASE)
_BAD_INFO_TITLE = re.compile(r"(\.(pdf|docx?|tex|dvi|ps)$)|microsoft word|untitled|^doi:|^\S+$", re.IGNORECASE)

if pymupdf is not None:
    _TEXT_FLAGS = pymupdf.TEXTFLAGS_TEXT & ~pymupdf.TEXT_PRESERVE_LIGATURES
    _DICT_FLAGS = pymupdf.TEXTFLAGS_DICT & ~pymupdf.TEXT_PRESERVE_LIGATURES
# Without PyMuPDF only the first pages are read by default (DOI / arXiv id / title live there and pypdf is slow).
PYPDF_DEFAULT_MAX_PAGES = 3

_ATOM = "{http://www.w3.org/2005/Atom}"
_ARXIV_NS = "{http://arxiv.org/schemas/atom}"

_CROSSREF_TYPES = {
    "journal-article": "article",
    "proceedings-article": "inproceedings",
    "book-chapter": "incollection",
    "book-part": "incollection",
    "book-section": "incollection",
    "book": "book",
    "monograph": "book",
    "edited-book": "book",
    "reference-book": "book",
    "dissertation": "phdthesis",
    "report": "techreport",
    "posted-content": "misc",
    "dataset": "misc",
    "standard": "misc",
}
_OPENALEX_TYPES = {
    "article": "article",
    "review": "article",
    "letter": "article",
    "book-chapter": "incollection",
    "book": "book",
    "dissertation": "phdthesis",
    "preprint": "misc",
    "report": "techreport",
}


class PdfReadError(RuntimeError):
    pass


# --------------------------------------------------------------------------- identifiers
def clean_doi(raw: str) -> str:
    """Strip resolver prefixes and trailing punctuation; DOIs are case-insensitive -> lower-case."""
    doi = _DOI_PREFIX.sub("", (raw or "").strip())
    doi = doi.rstrip(".,;:")
    while doi.endswith(")") and doi.count(")") > doi.count("("):
        doi = doi[:-1].rstrip(".,;:")
    return doi.lower()


def find_dois(text: str) -> list[str]:
    seen: dict[str, None] = {}
    for match in DOI_PATTERN.finditer(text or ""):
        doi = clean_doi(match.group())
        if "/" in doi and len(doi.split("/", 1)[1]) >= 2:
            seen.setdefault(doi, None)
    return list(seen)


def find_arxiv_ids(text: str) -> list[str]:
    """Version-less arXiv identifiers, in order of appearance (requires an ``arXiv`` cue)."""
    hits: list[tuple[int, str]] = []
    for pattern in _ARXIV_PATTERNS:
        hits += [(m.start(), m.group(1)) for m in pattern.finditer(text or "")]
    return list(dict.fromkeys(aid for _, aid in sorted(hits)))


def arxiv_id_from_filename(filename: str) -> str | None:
    match = _ARXIV_FILENAME.fullmatch(Path(filename).stem) if filename else None
    return match.group(1) if match else None


# --------------------------------------------------------------------------- text helpers
def clean_markup(text: str) -> str:
    """Crossref/JATS/HTML markup -> LaTeX-friendly plain text (sub/superscripts become math)."""
    text = re.sub(r"<(sub|inf)>(.*?)</\1>", r"$_{\2}$", text or "", flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<sup>(.*?)</sup>", r"$^{\1}$", text, flags=re.IGNORECASE | re.DOTALL)
    text = _BLOCK_TAG.sub(" ", text)  # paragraph-level tags separate words
    text = re.sub(r"<[^>]+>", "", text)  # inline tags (<i>, <mml:mi>, ...) do not
    text = html.unescape(text)
    text = re.sub(r"\s+([,.;:)])", r"\1", " ".join(text.split()))
    return text


def _compact(text: str) -> str:
    return _NON_ALNUM.sub("", unidecode(text or "").lower())


def _normalize_for_match(text: str) -> str:
    return " ".join(_NON_ALNUM.sub(" ", unidecode(text or "").lower()).split())


def title_similarity(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, _normalize_for_match(a), _normalize_for_match(b)).ratio()


def title_printed_in(title: str, compact_text: str) -> bool:
    """True if ``title`` (first 80 significant chars) appears in the compacted page text."""
    needle = _compact(title)[:80]
    return len(needle) >= 8 and needle in compact_text


# --------------------------------------------------------------------------- PDF reading
@dataclass(slots=True)
class PdfContent:
    filename: str = ""
    page_texts: list[str] = field(default_factory=list)
    info: dict[str, str] = field(default_factory=dict)
    xmp: str = ""
    title_candidates: list[str] = field(default_factory=list)
    author_hint: str = ""

    @property
    def full_text(self) -> str:
        return "\n".join(self.page_texts)

    def page(self, index: int) -> str:
        return self.page_texts[index] if index < len(self.page_texts) else ""


def _plausible_title(text: str) -> bool:
    text = " ".join((text or "").split())
    return 10 <= len(text) <= 300 and len(text.split()) >= 2 and not _BAD_INFO_TITLE.search(text)


def _largest_font_title(page: Any) -> tuple[str, str]:
    """(title, author line) from the largest horizontal font on the upper 60% of page 1."""
    data = page.get_text("dict", flags=_DICT_FLAGS)
    height = page.rect.height
    lines: list[tuple[float, float, str]] = []
    for block in data.get("blocks", []):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            dx, dy = line.get("dir", (1.0, 0.0))
            if abs(dy) > 0.01 or dx <= 0:  # rotated text, e.g. the arXiv side stamp
                continue
            spans = [s for s in line.get("spans", []) if s.get("text", "").strip()]
            y0 = float(line["bbox"][1])
            if not spans or y0 > 0.6 * height:
                continue
            text = " ".join("".join(s["text"] for s in spans).split())
            lines.append((y0, max(float(s["size"]) for s in spans), text))
    if not lines:
        return "", ""
    max_size = max(size for _, size, _ in lines)
    title_lines = sorted((ln for ln in lines if ln[1] >= max_size - 0.5), key=lambda ln: ln[0])
    title = " ".join(t for _, _, t in title_lines)
    below = sorted((ln for ln in lines if ln[0] > title_lines[-1][0] and ln[1] < max_size - 0.5), key=lambda ln: ln[0])
    return title, (below[0][2] if below else "")


def read_pdf(path: Path, *, max_pages: int | None = None) -> PdfContent:
    """Extract text of every page (or ``max_pages``), info dict, XMP and title candidates."""
    path = Path(path)
    if pymupdf is None:
        return _read_pdf_pypdf(path, max_pages=max_pages)
    try:
        with pymupdf.open(path) as doc:
            if doc.needs_pass:
                raise PdfReadError(f"Encrypted PDF: {path.name}")
            pages = doc.page_count if max_pages is None else min(max_pages, doc.page_count)
            texts = [doc[i].get_text("text", flags=_TEXT_FLAGS) for i in range(pages)]
            info = {k: str(v) for k, v in (doc.metadata or {}).items() if v and isinstance(v, str)}
            xmp = doc.get_xml_metadata() or ""
            candidates: list[str] = []
            author_hint = ""
            if _plausible_title(info.get("title", "")):
                candidates.append(" ".join(info["title"].split()))
            if doc.page_count:
                title, author_hint = _largest_font_title(doc[0])
                if _plausible_title(title) and title not in candidates:
                    candidates.append(title)
    except PdfReadError:
        raise
    except Exception as exc:  # PyMuPDF raises several unrelated types on corrupt input
        raise PdfReadError(f"Cannot read {path.name}: {exc}") from exc
    return PdfContent(
        filename=path.name,
        page_texts=texts,
        info=info,
        xmp=xmp,
        title_candidates=candidates,
        author_hint=author_hint,
    )


def _read_pdf_pypdf(path: Path, *, max_pages: int | None = None) -> PdfContent:
    """Fallback reader (pure Python): text of the first pages and the info dictionary; no font-size title."""
    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover - neither back end installed
        raise PdfReadError("No PDF reader available (install PyMuPDF or pypdf)") from exc
    try:
        reader = PdfReader(str(path))
        if reader.is_encrypted:
            try:
                unlocked = bool(reader.decrypt(""))
            except Exception:  # e.g. AES without the cryptography package
                unlocked = False
            if not unlocked:
                raise PdfReadError(f"Encrypted PDF: {path.name}")
        total = len(reader.pages)
        limit = PYPDF_DEFAULT_MAX_PAGES if max_pages is None else max_pages
        texts = []
        for index in range(min(limit, total)):
            try:
                texts.append(reader.pages[index].extract_text() or "")
            except Exception:  # one unreadable page must not lose the others
                texts.append("")
        info = {}
        for key, value in dict(reader.metadata or {}).items():
            if isinstance(value, str) and value:
                info[str(key).lstrip("/").lower()] = value
        xmp = ""
        try:
            raw = reader.trailer["/Root"].get("/Metadata")
            if raw is not None:
                xmp = raw.get_object().get_data().decode("utf-8", "replace")
        except Exception:
            xmp = ""
    except PdfReadError:
        raise
    except Exception as exc:
        raise PdfReadError(f"Cannot read {path.name}: {exc}") from exc
    candidates = [" ".join(info["title"].split())] if _plausible_title(info.get("title", "")) else []
    return PdfContent(filename=path.name, page_texts=texts, info=info, xmp=xmp, title_candidates=candidates)


@dataclass(slots=True)
class PdfOutline:
    page_count: int = 0
    toc: list[tuple[int, str, int]] = field(default_factory=list)  # (level, title, page)


def pdf_outline(path: Path) -> PdfOutline:
    """Page count and bookmarks (GUI "Contents" tab). Blocking: call from a worker."""
    if pymupdf is None:
        raise PdfReadError("The outline needs PyMuPDF")
    try:
        with pymupdf.open(Path(path)) as doc:
            return PdfOutline(
                doc.page_count, [(int(lvl), str(title), int(page)) for lvl, title, page, *_ in doc.get_toc()]
            )
    except Exception as exc:  # corrupt/encrypted files: no outline rather than a crash
        raise PdfReadError(f"Cannot read {Path(path).name}: {exc}") from exc


# --------------------------------------------------------------------------- API payload parsers
def _first(value: Any) -> str:
    if isinstance(value, list):
        return str(value[0]) if value else ""
    return str(value or "")


def _crossref_year(message: Mapping[str, Any]) -> int | None:
    for key in ("published-print", "issued", "published", "published-online", "created"):
        parts = (message.get(key) or {}).get("date-parts") or [[None]]
        try:
            year = coerce_year(parts[0][0])
        except (ValueError, IndexError, TypeError):
            continue
        if year is not None:
            return year
    return None


def parse_crossref_message(message: Mapping[str, Any]) -> PaperMetadata:
    title = clean_markup(_first(message.get("title")))
    subtitle = clean_markup(_first(message.get("subtitle")))
    if subtitle and subtitle.lower() not in title.lower():
        title = f"{title}: {subtitle}" if title else subtitle
    authors = [
        Author(
            family=str(a.get("family") or "").strip(),
            given=str(a.get("given") or "").strip(),
            suffix=str(a.get("suffix") or "").strip(),
            literal=str(a.get("name") or "").strip(),
        )
        for a in message.get("author") or []
    ]
    abstract = clean_markup(message.get("abstract") or "")
    abstract = re.sub(r"^abstract\s*", "", abstract, flags=re.IGNORECASE)
    return PaperMetadata(
        title=title,
        authors=[a for a in authors if a.key_name],
        year=_crossref_year(message),
        journal=clean_markup(_first(message.get("container-title"))),
        volume=str(message.get("volume") or ""),
        issue=str(message.get("issue") or ""),
        pages=str(message.get("page") or ""),
        publisher=clean_markup(str(message.get("publisher") or "")),
        doi=clean_doi(str(message.get("DOI") or "")),
        entry_type=_CROSSREF_TYPES.get(str(message.get("type") or ""), "article"),
        abstract=abstract,
        url=str(message.get("URL") or ""),
        isbn="; ".join(dict.fromkeys(str(x) for x in message.get("ISBN") or [])),
        issn="; ".join(dict.fromkeys(str(x) for x in message.get("ISSN") or [])),
    )


def parse_arxiv_feed(xml_text: str) -> PaperMetadata | None:
    root = ET.fromstring(xml_text)
    entry = root.find(f"{_ATOM}entry")
    if entry is None:
        return None
    entry_id = entry.findtext(f"{_ATOM}id", "")
    if "/api/errors" in entry_id or not entry_id:
        return None
    match = re.search(r"arxiv\.org/abs/(.+?)(?:v\d+)?$", entry_id)
    published = entry.findtext(f"{_ATOM}published", "") or ""
    try:
        year = coerce_year(published[:4]) if published else None
    except ValueError:
        year = None
    return PaperMetadata(
        title=" ".join((entry.findtext(f"{_ATOM}title", "") or "").split()),
        authors=[Author.from_full_name(a.findtext(f"{_ATOM}name", "") or "") for a in entry.findall(f"{_ATOM}author")],
        year=year,
        doi=clean_doi(entry.findtext(f"{_ARXIV_NS}doi", "") or ""),
        arxiv_id=match.group(1) if match else "",
        entry_type="misc",
        abstract=" ".join((entry.findtext(f"{_ATOM}summary", "") or "").split()),
    )


def _openalex_abstract(inverted: Mapping[str, list[int]] | None) -> str:
    if not inverted:
        return ""
    positions = {pos: word for word, idxs in inverted.items() for pos in idxs}
    return " ".join(positions[i] for i in sorted(positions))


def parse_openalex_work(work: Mapping[str, Any]) -> PaperMetadata:
    location = work.get("primary_location") or {}
    source = location.get("source") or {}
    biblio = work.get("biblio") or {}
    first_page, last_page = biblio.get("first_page"), biblio.get("last_page")
    pages = f"{first_page}--{last_page}" if first_page and last_page and first_page != last_page else (first_page or "")
    try:
        year = coerce_year(work.get("publication_year"))
    except ValueError:
        year = None
    authors = [
        Author.from_full_name(((a.get("author") or {}).get("display_name")) or a.get("raw_author_name") or "")
        for a in work.get("authorships") or []
    ]
    return PaperMetadata(
        title=clean_markup(work.get("title") or work.get("display_name") or ""),
        authors=[a for a in authors if a.key_name],
        year=year,
        journal=str(source.get("display_name") or ""),
        volume=str(biblio.get("volume") or ""),
        issue=str(biblio.get("issue") or ""),
        pages=str(pages),
        doi=clean_doi(str(work.get("doi") or "")),
        entry_type=_OPENALEX_TYPES.get(str(work.get("type") or ""), "article"),
        abstract=_openalex_abstract(work.get("abstract_inverted_index")),
        keywords="; ".join(
            str(k.get("display_name"))
            for k in work.get("keywords") or []
            if isinstance(k, dict) and k.get("display_name")
        ),
        pmid=str((work.get("ids") or {}).get("pmid") or "").rsplit("/", 1)[-1],
    )


# --------------------------------------------------------------------------- resolver
@dataclass(slots=True)
class Resolution:
    metadata: PaperMetadata | None
    source: str = "none"
    needs_review: bool = True
    log: list[str] = field(default_factory=list)


class MetadataResolver:
    """Blocking HTTP resolver. Inject ``client`` (e.g. ``httpx.MockTransport``) for tests."""

    CROSSREF_WORKS = "https://api.crossref.org/works/"
    ARXIV_QUERY = "https://export.arxiv.org/api/query"
    OPENALEX_WORKS = "https://api.openalex.org/works"

    def __init__(
        self,
        *,
        user_agent: str = "JRef/1.0 (mailto:user@example.com)",
        timeout: float = 15.0,
        min_title_similarity: float = 0.85,
        client: httpx.Client | None = None,
    ) -> None:
        self.user_agent = user_agent
        self.min_title_similarity = min_title_similarity
        self._client = client or httpx.Client(timeout=timeout, follow_redirects=True)
        self._owns_client = client is None

    @classmethod
    def from_config(cls, config: Any, *, client: httpx.Client | None = None) -> MetadataResolver:
        return cls(
            user_agent=config.user_agent,
            timeout=config.http_timeout_s,
            min_title_similarity=config.openalex_min_title_similarity,
            client=client,
        )

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> MetadataResolver:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _get(
        self, url: str, *, params: Mapping[str, str] | None = None, accept: str = "application/json"
    ) -> httpx.Response | None:
        headers = {"User-Agent": self.user_agent, "Accept": accept}
        try:
            response = self._client.get(url, params=params, headers=headers)
        except httpx.HTTPError as exc:
            log.warning("HTTP error for %s: %s", url, exc)
            return None
        if response.status_code != 200:
            log.info("HTTP %s for %s", response.status_code, response.request.url)
            return None
        return response

    # -- single-source lookups (also used by the GUI "Fetch by DOI" button)
    def fetch_crossref(self, doi: str) -> PaperMetadata | None:
        response = self._get(self.CROSSREF_WORKS + quote(clean_doi(doi), safe="/"))
        if response is None:
            return None
        try:
            meta = parse_crossref_message(response.json()["message"])
        except (ValueError, KeyError, TypeError) as exc:
            log.warning("Malformed Crossref payload for %s: %s", doi, exc)
            return None
        return meta if (meta.title or meta.authors) else None

    def fetch_arxiv(self, arxiv_id: str) -> PaperMetadata | None:
        response = self._get(
            self.ARXIV_QUERY, params={"id_list": arxiv_id, "max_results": "1"}, accept="application/atom+xml"
        )
        if response is None:
            return None
        try:
            meta = parse_arxiv_feed(response.text)
        except ET.ParseError as exc:
            log.warning("Malformed arXiv feed for %s: %s", arxiv_id, exc)
            return None
        if meta is not None and not meta.arxiv_id:
            meta.arxiv_id = arxiv_id
        return meta

    def search_openalex(self, title: str) -> tuple[PaperMetadata, float] | None:
        response = self._get(self.OPENALEX_WORKS, params={"search": title, "per-page": "5"})
        if response is None:
            return None
        try:
            results = response.json().get("results") or []
        except ValueError:
            return None
        scored = [(title_similarity(title, w.get("title") or w.get("display_name") or ""), w) for w in results]
        if not scored:
            return None
        score, work = max(scored, key=lambda item: item[0])
        if score < self.min_title_similarity:
            log.info("OpenAlex best match %.2f below threshold for %r", score, title)
            return None
        return parse_openalex_work(work), score

    def lookup(self, *, doi: str = "", arxiv_id: str = "", title: str = "") -> tuple[PaperMetadata | None, str]:
        """Manual lookup for an existing entry (GUI "Fetch details"): DOI, then arXiv, then title."""
        if doi:
            meta = self.fetch_crossref(doi)
            if meta is not None:
                return meta, "Crossref"
        if arxiv_id:
            found = self._via_arxiv(arxiv_id)
            if found:
                return found[0], "arXiv" if found[1] == "arxiv" else "arXiv + Crossref"
        if title:
            hit = self.search_openalex(title)
            if hit is not None:
                meta = hit[0]
                if meta.doi:
                    published = self.fetch_crossref(meta.doi)
                    if published is not None:
                        return published, "OpenAlex + Crossref"
                return meta, "OpenAlex"
        return None, ""

    # -- pipeline
    def _via_arxiv(self, arxiv_id: str) -> tuple[PaperMetadata, str] | None:
        meta = self.fetch_arxiv(arxiv_id)
        if meta is None:
            return None
        if meta.doi:
            published = self.fetch_crossref(meta.doi)
            if published is not None:
                published.arxiv_id = arxiv_id
                if not published.abstract:
                    published.abstract = meta.abstract
                return published, "arxiv+crossref"
        return meta, "arxiv"

    def resolve(self, content: PdfContent) -> Resolution:
        result = Resolution(metadata=None)
        page1, page2 = content.page(0), content.page(1)
        primary_text = "\n".join([content.xmp, *content.info.values(), page1])
        primary_dois = find_dois(primary_text)
        primary_arxiv = find_arxiv_ids(primary_text)
        from_name = arxiv_id_from_filename(content.filename)
        if from_name and from_name not in primary_arxiv:
            primary_arxiv.append(from_name)
        secondary_dois = [d for d in find_dois(page2) if d not in primary_dois]
        secondary_arxiv = [a for a in find_arxiv_ids(page2) if a not in primary_arxiv]
        # The paper's own title is printed on page 1; page 2 may hold the reference list.
        compact_pages = _compact(page1)
        result.log.append(
            f"DOIs p1/XMP={primary_dois} p2={secondary_dois}; arXiv p1/XMP={primary_arxiv} p2={secondary_arxiv}"
        )

        def done(meta: PaperMetadata, source: str, *, review: bool = False) -> Resolution:
            if not meta.arxiv_id and primary_arxiv:
                meta.arxiv_id = primary_arxiv[0]
            result.metadata, result.source, result.needs_review = (
                meta,
                source,
                review or not meta.is_complete_for_naming(),
            )
            result.log.append(f"resolved via {source}")
            return result

        unverified: PaperMetadata | None = None
        for doi in primary_dois[:3]:
            meta = self.fetch_crossref(doi)
            if meta is None:
                result.log.append(f"Crossref: no record for {doi}")
            elif title_printed_in(meta.title, compact_pages):
                return done(meta, "crossref")
            else:
                result.log.append(f"Crossref title for {doi} not found on page 1")
                unverified = unverified or meta
        for arxiv_id in primary_arxiv[:2]:
            found = self._via_arxiv(arxiv_id)
            if found:
                return done(*found)
        if unverified is not None:
            return done(unverified, "crossref", review=True)
        for doi in secondary_dois[:3]:
            meta = self.fetch_crossref(doi)
            if meta is not None and title_printed_in(meta.title, compact_pages):
                return done(meta, "crossref")
        for arxiv_id in secondary_arxiv[:2]:
            found = self._via_arxiv(arxiv_id)
            if found and title_printed_in(found[0].title, compact_pages):
                return done(*found)
        for candidate in content.title_candidates[:2]:
            hit = self.search_openalex(candidate)
            if hit is None:
                result.log.append(f"OpenAlex: no confident match for {candidate!r}")
                continue
            meta, score = hit
            result.log.append(f"OpenAlex match (similarity {score:.2f})")
            if meta.doi:
                published = self.fetch_crossref(meta.doi)
                if published is not None:
                    return done(published, "openalex+crossref")
            return done(meta, "openalex")
        result.log.append("all metadata sources failed")
        return result
