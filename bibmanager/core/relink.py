"""Find the PDFs of entries that have none, inside a folder (e.g. the folder Mendeley organised).

Pure Python, no SQLite: it works on ``Paper`` snapshots and the file system, so it can run in a worker
thread. Matching rules, strongest first (a PDF is given to at most one entry; ties are reported, not guessed):

1. ``recorded`` -- the file name recorded at import (Mendeley's ``file`` field) is found in the folder;
2. ``key``      -- the file is named after the citation key (``Davidovitch2011.pdf``, ``Anand_2023.pdf``);
3. ``title``    -- Mendeley-style name ``Authors - Year - Title.pdf`` whose (possibly truncated) title and year
                   match the entry;
4. ``fuzzy``    -- the file name contains the first 30 characters of the entry's title (and its year, if any).
"""

from __future__ import annotations

import os
import re
import unicodedata
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath, PureWindowsPath

from unidecode import unidecode

from .models import Paper

_NON_ALNUM = re.compile(r"[^0-9a-z]+")
_YEAR = re.compile(r"(?<!\d)(1[5-9]\d\d|20\d\d)(?!\d)")
MIN_TITLE_PREFIX = 12  # Mendeley truncates long titles in file names; shorter fragments are too ambiguous
FUZZY_PREFIX = 30
RULES = ("recorded", "key", "title", "fuzzy")


def norm(text: str) -> str:
    return _NON_ALNUM.sub("", unidecode(unicodedata.normalize("NFC", text or "")).lower())


@dataclass(slots=True)
class PdfFile:
    path: Path
    name_key: str  # case-folded file name
    stem_norm: str
    year: int | None
    title_norm: str  # title part of "Authors - Year - Title", else ""


@dataclass(slots=True)
class RelinkMatch:
    paper_id: int
    path: Path
    rule: str


@dataclass(slots=True)
class RelinkResult:
    matches: list[RelinkMatch] = field(default_factory=list)
    ambiguous: list[tuple[int, list[Path]]] = field(default_factory=list)
    scanned: int = 0  # PDFs found in the folder(s)
    searched: int = 0  # entries without a PDF


def _parse(path: Path) -> PdfFile:
    stem = path.stem
    parts = [p.strip() for p in stem.split(" - ")]
    year: int | None = None
    title = ""
    if len(parts) >= 3 and _YEAR.fullmatch(parts[1] or ""):  # Mendeley: "Authors - Year - Title"
        year = int(parts[1])
        title = " - ".join(parts[2:])
    else:
        found = _YEAR.search(stem)
        year = int(found.group(1)) if found else None
    return PdfFile(path, unicodedata.normalize("NFC", path.name).casefold(), norm(stem), year, norm(title))


def scan_pdfs(folders: Iterable[Path], *, exclude: Iterable[Path] = ()) -> list[PdfFile]:
    excluded = {os.path.normcase(os.path.abspath(p)) for p in exclude}
    files: list[PdfFile] = []
    for folder in folders:
        for dirpath, _dirnames, filenames in os.walk(folder):
            for name in filenames:
                if name.lower().endswith(".pdf"):
                    path = Path(dirpath, name)
                    if os.path.normcase(os.path.abspath(path)) not in excluded:
                        files.append(_parse(path))
    return files


def _recorded_names(paper: Paper) -> list[str]:
    raw = paper.meta.extra_fields.get("file", "")
    names = []
    for item in re.split(r"[;\n]", raw):
        item = item.strip()
        if item:
            name = PureWindowsPath(item).name if "\\" in item else PurePosixPath(item).name
            names.append(unicodedata.normalize("NFC", name).casefold())
    return names


def find_missing_pdfs(
    papers: Sequence[Paper], folders: Iterable[Path], *, exclude: Iterable[Path] = ()
) -> RelinkResult:
    """Match entries without a PDF to files in ``folders`` (``exclude``: files already in the library)."""
    pdfs = scan_pdfs(folders, exclude=exclude)
    targets = [p for p in papers if not p.has_pdf]
    result = RelinkResult(scanned=len(pdfs), searched=len(targets))
    by_name: dict[str, list[PdfFile]] = defaultdict(list)
    by_stem: dict[str, list[PdfFile]] = defaultdict(list)
    by_year: dict[int | None, list[PdfFile]] = defaultdict(list)
    for pdf in pdfs:
        by_name[pdf.name_key].append(pdf)
        by_stem[pdf.stem_norm].append(pdf)
        by_year[pdf.year].append(pdf)

    proposals: dict[int, tuple[int, list[PdfFile]]] = {}  # paper id -> (rule rank, candidates)
    for paper in targets:
        title = norm(paper.meta.title)
        year = paper.meta.year
        rules: list[list[PdfFile]] = [
            [pdf for name in _recorded_names(paper) for pdf in by_name.get(name, [])],
            by_stem.get(norm(paper.citation_key), []) if norm(paper.citation_key) else [],
            [
                pdf
                for pdf in by_year.get(year, [])
                if pdf.title_norm
                and len(pdf.title_norm) >= min(MIN_TITLE_PREFIX, len(title))
                and title
                and (title == pdf.title_norm or title.startswith(pdf.title_norm))
            ]
            if year is not None
            else [],
            [
                pdf
                for pdf in pdfs
                if len(title) >= FUZZY_PREFIX
                and title[:FUZZY_PREFIX] in pdf.stem_norm
                and (year is None or pdf.year in (None, year))
            ],
        ]
        for rank, found in enumerate(rules):
            unique = list({id(p): p for p in found}.values())
            if unique:
                proposals[paper.id] = (rank, unique)
                break

    # Resolve: strongest rules first; a file goes to one entry only; ties are reported, never guessed.
    claims: dict[Path, list[tuple[int, int]]] = defaultdict(list)  # file -> [(rule rank, paper id)]
    for paper_id, (rank, found) in proposals.items():
        if len(found) == 1:
            claims[found[0].path].append((rank, paper_id))
    used: set[Path] = set()
    for paper_id, (rank, found) in sorted(proposals.items(), key=lambda item: (item[1][0], item[0])):
        free = [p for p in found if p.path not in used]
        if len(free) == 1:
            path = free[0].path
            if any(r == rank and pid != paper_id for r, pid in claims[path]):  # same evidence for two entries
                result.ambiguous.append((paper_id, [path]))
                continue
            used.add(path)
            result.matches.append(RelinkMatch(paper_id, path, RULES[rank]))
        elif len(free) > 1:
            result.ambiguous.append((paper_id, [p.path for p in free]))
    return result
