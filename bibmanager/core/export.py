"""ZIP export of a project's PDFs, to share them with colleagues (after 0.8.0).

Two steps, following the threading contract of ``library.py``:

* ``Library.plan_project_zip`` reads SQLite and checks which files are on this device (GUI thread);
* :func:`write_zip` only reads those files and writes the archive, so it is safe in a worker thread.

The library is never modified: files are read where they are (managed ``pdfs/<YYYY>/`` or linked), and the archive is
written to ``<name>.part`` next to the target, renamed when complete and deleted on error or cancellation.

Layout: one folder named after the project's ``.bib`` slug; every PDF is named after its citation key (linked PDFs
too, whatever their name on disk), so the files match the keys of the enclosed ``.bib``::

    <slug>/<slug>.bib            the project's entries (optional)
    <slug>/<key>.pdf             main PDF of each reference
    <slug>/<key>_S<n>.<ext>      supplementary files (optional)
"""

from __future__ import annotations

import os
import threading
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

ZIP_CHUNK = 1 << 20  # bytes copied between two progress reports / cancellation checks
PART_SUFFIX = ".part"
# Formats that are already compressed: stored as they are (deflating them costs time and gains next to nothing).
STORED_SUFFIXES = frozenset(
    {
        ".pdf", ".zip", ".gz", ".tgz", ".bz2", ".xz", ".7z", ".rar", ".png", ".jpg", ".jpeg", ".gif", ".webp",
        ".tif", ".tiff", ".mp4", ".m4v", ".mov", ".avi", ".mkv", ".webm", ".mp3", ".m4a", ".ogg", ".flac",
        ".docx", ".xlsx", ".pptx", ".odt", ".ods", ".odp", ".epub", ".h5", ".hdf5", ".npz", ".mat",
    }
)  # fmt: skip

ProgressCallback = Callable[[int, int], None]  # bytes done, bytes in total


@dataclass(frozen=True, slots=True)
class ZipOptions:
    include_bib: bool = True
    include_supplements: bool = False
    include_subprojects: bool = False


@dataclass(frozen=True, slots=True)
class ZipMember:
    source: Path
    arcname: str
    citation_key: str
    size: int
    supplement: bool = False


@dataclass(slots=True)
class ZipPlan:
    """What a ZIP of a project will contain; built by ``Library.plan_project_zip``."""

    name: str  # project name (dialog titles, reports)
    folder: str  # top-level folder inside the archive = the project's .bib slug
    references: int = 0
    members: list[ZipMember] = field(default_factory=list)
    bib_text: str | None = None
    without_pdf: list[str] = field(default_factory=list)  # keys of references with no PDF at all
    unavailable: list[str] = field(default_factory=list)  # recorded but not on this device ("key" / "key (label)")
    supplements_found: int = 0  # supplementary files on this device, included or not (shown by the dialog)
    supplements_bytes: int = 0
    subprojects: int = 0  # nested sub-projects of the project, included or not

    @property
    def pdfs(self) -> int:
        return sum(1 for m in self.members if not m.supplement)

    @property
    def supplements(self) -> int:
        return sum(1 for m in self.members if m.supplement)

    @property
    def total_bytes(self) -> int:
        return sum(m.size for m in self.members) + len((self.bib_text or "").encode("utf-8"))

    @property
    def bib_arcname(self) -> str:
        return f"{self.folder}/{self.folder}.bib"

    @property
    def default_filename(self) -> str:
        return f"{self.folder}.zip"

    @property
    def missing(self) -> list[str]:
        """References (or supplementary files) that will not be in the archive, for the report."""
        return [f"{k} (no PDF)" for k in self.without_pdf] + [f"{k} (not on this device)" for k in self.unavailable]


@dataclass(slots=True)
class ZipReport:
    target: Path
    pdfs: int = 0
    supplements: int = 0
    bib: bool = False
    size: int = 0  # size of the archive on disk
    skipped: list[str] = field(default_factory=list)  # files that could not be opened ("name: reason")
    cancelled: bool = False


def unique_arcname(taken: set[str], name: str) -> str:
    """``name``, or ``stem-2.ext``, ``stem-3.ext``... if already used (case-insensitively: Windows/macOS unzip)."""
    candidate, n = name, 1
    stem, dot, ext = name.rpartition(".")
    if not dot or "/" in ext:
        stem, dot, ext = name, "", ""
    while candidate.casefold() in taken:
        n += 1
        candidate = f"{stem}-{n}{dot}{ext}"
    taken.add(candidate.casefold())
    return candidate


def file_size(path: Path) -> int | None:
    """Size of a regular file, ``None`` if it is not on this device (missing, not synchronised, unreadable)."""
    try:
        return path.stat().st_size if path.is_file() else None
    except OSError:
        return None


def format_size(size: int) -> str:
    value = float(size)
    for unit in ("bytes", "KB", "MB", "GB"):
        if value < 1000 or unit == "GB":
            return f"{int(value)} {unit}" if unit == "bytes" else f"{value:.1f} {unit}"
        value /= 1000
    return f"{value:.1f} GB"  # pragma: no cover - loop always returns


def _compression(arcname: str) -> int:
    return zipfile.ZIP_STORED if Path(arcname).suffix.lower() in STORED_SUFFIXES else zipfile.ZIP_DEFLATED


def write_zip(
    plan: ZipPlan,
    target: str | Path,
    *,
    progress: ProgressCallback | None = None,
    cancel: threading.Event | None = None,
) -> ZipReport:
    """Write the archive described by ``plan`` to ``target`` (worker thread: no database access).

    A file that cannot be opened any more (deleted or moved since the plan) is skipped and reported; any other error
    aborts the export. Nothing is left behind on error or cancellation, and an existing ``target`` is replaced only
    once the new archive is complete.
    """
    target = Path(target)
    identity = os.path.normcase(os.path.abspath(target))
    if any(os.path.normcase(os.path.abspath(m.source)) == identity for m in plan.members):
        raise ValueError(f"{target.name} is one of the files to export: choose another name for the archive")
    part = target.with_name(target.name + PART_SUFFIX)
    report = ZipReport(target)
    total = max(plan.total_bytes, 1)
    done = 0

    def tick(amount: int) -> bool:
        nonlocal done
        done += amount
        if progress is not None:
            progress(min(done, total), total)
        return cancel is not None and cancel.is_set()

    try:
        with zipfile.ZipFile(part, "w", allowZip64=True, strict_timestamps=False) as archive:
            if plan.bib_text is not None:
                data = plan.bib_text.encode("utf-8")
                archive.writestr(plan.bib_arcname, data, compress_type=zipfile.ZIP_DEFLATED)
                report.bib = True
                if tick(len(data)):
                    report.cancelled = True
            for member in plan.members:
                if report.cancelled:
                    break
                try:
                    source = open(member.source, "rb")  # noqa: SIM115 - closed below, errors reported per file
                    info = zipfile.ZipInfo.from_file(member.source, member.arcname, strict_timestamps=False)
                except OSError as exc:
                    report.skipped.append(f"{member.arcname.rpartition('/')[2]}: {exc.strerror or exc}")
                    tick(member.size)
                    continue
                info.compress_type = _compression(member.arcname)
                # ``info.file_size`` (from the file) lets zipfile switch to ZIP64 by itself for files > 2 GB.
                with source, archive.open(info, "w") as out:
                    while chunk := source.read(ZIP_CHUNK):
                        out.write(chunk)
                        if tick(len(chunk)):
                            report.cancelled = True
                            break
                if report.cancelled:
                    break
                if member.supplement:
                    report.supplements += 1
                else:
                    report.pdfs += 1
        if report.cancelled:
            part.unlink(missing_ok=True)
            return report
        os.replace(part, target)
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    report.size = target.stat().st_size
    return report
