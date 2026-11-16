"""Manuscript check: the citation keys a LaTeX manuscript uses, compared with the entries of a project.

A project can also write its ``.bib`` file into the folder of the manuscript that cites it (its external export
path). The ``.tex`` files of that folder are scanned for citation commands -- ``\\cite``, ``\\citep``,
``\\citet``, ``\\parencite``, ``\\textcite``, ``\\autocite``, ``\\footcite``, ``\\nocite``, the multi-cite forms
``\\cites{a}{b}`` and every other command whose name contains ``cite`` -- with optional ``[pre][post]``
arguments and starred forms. Comments (``%`` to the end of the line) are ignored. The result says which cited
keys are missing from the project (and whether the library has them) and which project entries are never cited.

Scanning reads files only (no database), so it can run in a worker thread; ``compare`` then works on key sets.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

MAX_FILES = 300
MAX_FILE_BYTES = 5_000_000
MAX_DEPTH = 3
_SKIPPED_DIRS = frozenset({"build", "out", "_build", "node_modules", "__pycache__"})
_COMMAND = re.compile(r"\\([A-Za-z]*[Cc]ite[A-Za-z]*)\*?")
_COMMENT = re.compile(r"(?<!\\)%.*")


@dataclass(slots=True)
class CitationScan:
    folder: Path
    tex_files: list[Path] = field(default_factory=list)
    keys: set[str] = field(default_factory=set)
    nocite_all: bool = False  # \nocite{*}: every entry of the .bib file is printed


@dataclass(slots=True)
class ManuscriptCheck:
    folder: Path
    tex_files: list[Path]
    cited: set[str]
    missing: list[str]  # cited but not in the project
    missing_in_library: list[str]  # ... of which the library has an entry (can be added to the project)
    unused: list[str]  # in the project, never cited
    nocite_all: bool = False

    @property
    def cited_in_project(self) -> int:
        return len(self.cited) - len(self.missing)


def tex_files(folder: Path) -> list[Path]:
    """``.tex`` files under ``folder`` (at most ``MAX_DEPTH`` levels, hidden and build folders skipped)."""
    folder = Path(folder)
    found: list[Path] = []
    base_depth = len(folder.parts)
    for root, dirs, files in os.walk(folder):
        depth = len(Path(root).parts) - base_depth
        dirs[:] = sorted(d for d in dirs if not d.startswith(".") and d.lower() not in _SKIPPED_DIRS)
        if depth >= MAX_DEPTH:
            dirs[:] = []
        for name in sorted(files):
            if name.lower().endswith(".tex"):
                found.append(Path(root) / name)
                if len(found) >= MAX_FILES:
                    return found
    return found


def _braced(text: str, start: int) -> tuple[str, int] | None:
    """The content of the ``{...}`` group starting at ``start`` (nested braces allowed) and the index after it."""
    if start >= len(text) or text[start] != "{":
        return None
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[start + 1 : i], i + 1
    return None


def _skip_space(text: str, i: int) -> int:
    while i < len(text) and text[i] in " \t\r\n":
        i += 1
    return i


def citation_keys(text: str) -> tuple[set[str], bool]:
    """Keys cited in LaTeX source ``text``; the flag is true for ``\\nocite{*}``."""
    text = "\n".join(_COMMENT.sub("", line) for line in text.splitlines())
    keys: set[str] = set()
    everything = False
    for match in _COMMAND.finditer(text):
        command = match.group(1)
        i = match.end()
        multi = command.endswith("cites")  # biblatex multi-cite: \cites[pre]{a}[post]{b}
        while True:
            i = _skip_space(text, i)
            if i < len(text) and text[i] in "[(":  # optional arguments, also biblatex's (pre)(post) of \cites
                close = "]" if text[i] == "[" else ")"
                end = text.find(close, i)
                if end < 0:
                    break
                i = end + 1
                continue
            group = _braced(text, i)
            if group is None:
                break
            content, i = group
            for key in content.split(","):
                key = key.strip()
                if key == "*":
                    everything = True
                elif key and not key.startswith("\\"):
                    keys.add(key)
            if not multi:
                break
    return keys, everything


def scan_citations(folder: Path) -> CitationScan:
    """Read every ``.tex`` file of ``folder`` and collect the cited keys."""
    scan = CitationScan(Path(folder))
    if not scan.folder.is_dir():
        return scan
    for path in tex_files(scan.folder):
        try:
            if path.stat().st_size > MAX_FILE_BYTES:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        keys, everything = citation_keys(text)
        scan.tex_files.append(path)
        scan.keys |= keys
        scan.nocite_all = scan.nocite_all or everything
    return scan


def compare(scan: CitationScan, project_keys: Iterable[str], library_keys: Iterable[str]) -> ManuscriptCheck:
    """Keys are compared without regard to case, as BibTeX does (JRef's keys are unique in that sense too)."""
    project = {k.casefold(): k for k in project_keys}
    library = {k.casefold() for k in library_keys}
    cited = {k.casefold() for k in scan.keys}
    missing = sorted((k for k in scan.keys if k.casefold() not in project), key=str.casefold)
    unused = sorted((k for f, k in project.items() if f not in cited), key=str.casefold)
    return ManuscriptCheck(
        folder=scan.folder,
        tex_files=list(scan.tex_files),
        cited=set(scan.keys),
        missing=missing,
        missing_in_library=[k for k in missing if k.casefold() in library],
        unused=[] if scan.nocite_all else unused,
        nocite_all=scan.nocite_all,
    )


def manuscript_folder(export_path: str | None) -> Path | None:
    """The folder of a project's external ``.bib`` file, where its manuscript is expected."""
    if not export_path:
        return None
    folder = Path(export_path).expanduser().parent
    return folder if folder.is_dir() else None
