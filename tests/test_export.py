"""ZIP export of a project's PDFs (``Library.plan_project_zip`` + ``core.export.write_zip``)."""

from __future__ import annotations

import shutil
import threading
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from bibmanager.core import Library, ReferenceCandidate
from bibmanager.core.export import ZipOptions, unique_arcname, write_zip

from .test_core import bib_entries, meta, pdfs_on_disk


def _ingest(library: Library, make_pdf: Callable[..., Path], *surnames: str, year: int, folder: int, **kw: Any) -> int:
    return library.ingest_pdf(make_pdf(), folder, metadata=meta(*surnames, year=year, **kw)).paper.id


def _names(path: Path) -> list[str]:
    with zipfile.ZipFile(path) as archive:
        assert archive.testzip() is None  # every CRC is right
        return sorted(archive.namelist())


def test_zip_holds_the_project_pdfs_named_after_their_keys_and_the_bib(
    library: Library, make_pdf: Callable[..., Path], tmp_path: Path
) -> None:
    project = library.create_collection("Soft robotics")
    other = library.create_collection("Other paper")
    a = _ingest(library, make_pdf, "Anand", year=2023, folder=project.id, title="PINNs for RVE homogenisation")
    b = _ingest(library, make_pdf, "Ciambella", "Lancioni", year=2025, folder=project.id)
    _ingest(library, make_pdf, "Rossi", year=2020, folder=other.id)  # not in the project
    library.add_to_collection(a, other.id)  # shared with another project: still one file on disk, one in the zip
    before = pdfs_on_disk(library.root)

    plan = library.plan_project_zip(project.id)
    assert plan.references == 2 and plan.pdfs == 2 and plan.missing == []
    target = tmp_path / "out" / plan.default_filename
    target.parent.mkdir()
    progress: list[tuple[int, int]] = []
    report = write_zip(plan, target, progress=lambda done, total: progress.append((done, total)))

    assert _names(target) == ["soft_robotics/Anand_2023.pdf", "soft_robotics/Ciambella_Lancioni_2025.pdf",
                              "soft_robotics/soft_robotics.bib"]  # fmt: skip
    assert report.pdfs == 2 and report.bib and report.size == target.stat().st_size and not report.cancelled
    assert progress[-1][0] == progress[-1][1] == plan.total_bytes
    with zipfile.ZipFile(target) as archive:
        assert archive.read("soft_robotics/Anand_2023.pdf") == library.pdf_path(library.get_paper(a)).read_bytes()
        assert archive.getinfo("soft_robotics/Anand_2023.pdf").compress_type == zipfile.ZIP_STORED
        archive.extract("soft_robotics/soft_robotics.bib", tmp_path / "x")
    entries = bib_entries(tmp_path / "x" / "soft_robotics" / "soft_robotics.bib")
    assert set(entries) == {"Anand_2023", "Ciambella_Lancioni_2025"}
    assert "{PINNs}" in entries["Anand_2023"]["title"] and "{RVE}" in entries["Anand_2023"]["title"]
    # The library is untouched: same PDFs, nothing written next to the archive but the archive.
    assert pdfs_on_disk(library.root) == before
    assert [p.name for p in target.parent.iterdir()] == [target.name]
    assert library.get_paper(b).relative_pdf_path == "pdfs/2025/Ciambella_Lancioni_2025.pdf"


def test_linked_pdfs_are_renamed_in_the_zip_and_missing_files_are_reported(
    library: Library, make_pdf: Callable[..., Path], tmp_path: Path
) -> None:
    project = library.create_collection("Review")
    mendeley = tmp_path / "Mendeley Desktop"
    mendeley.mkdir()
    linked = mendeley / "Gu et al. - 2013 - Surface wrinkling.pdf"
    shutil.copy2(make_pdf(), linked)
    gu = library.commit_reference(ReferenceCandidate("Gu2013", meta("Gu", year=2013)), folder_ids=[project.id]).paper
    library.attach_pdf(gu.id, library.prepare_attachment(linked), mode="link")
    library.commit_reference(ReferenceCandidate("Cao2012", meta("Cao", year=2012)), folder_ids=[project.id])  # no PDF
    gone = _ingest(library, make_pdf, "Wang", year=2019, folder=project.id)
    library.pdf_path(library.get_paper(gone)).unlink()  # recorded, but not on this device (not synchronised yet)

    plan = library.plan_project_zip(project.id, ZipOptions(include_bib=False))
    assert plan.without_pdf == ["Cao2012"] and plan.unavailable == ["Wang_2019"]
    assert plan.missing == ["Cao2012 (no PDF)", "Wang_2019 (not on this device)"]
    report = write_zip(plan, tmp_path / "review.zip")
    assert _names(tmp_path / "review.zip") == ["review/Gu2013.pdf"]
    assert report.pdfs == 1 and not report.bib
    assert linked.exists()  # read in place, never moved


def test_supplements_and_subprojects_are_optional(
    library: Library, make_pdf: Callable[..., Path], tmp_path: Path
) -> None:
    project = library.create_collection("Thesis")
    chapter = library.create_collection("Chapter 2", parent_id=project.id)
    main = _ingest(library, make_pdf, "Anand", year=2023, folder=project.id)
    data = tmp_path / "raw_data.CSV"
    data.write_text("t,u\n0,0\n", encoding="utf-8")
    library.add_supplement(main, library.prepare_attachment(data), mode="copy")
    nested = _ingest(library, make_pdf, "Ogden", year=1972, folder=chapter.id)
    library.add_to_collection(main, chapter.id)  # in both: exported once

    plain = library.plan_project_zip(project.id)
    assert (plain.pdfs, plain.supplements, plain.supplements_found, plain.subprojects) == (1, 0, 1, 1)
    full = library.plan_project_zip(
        project.id, ZipOptions(include_supplements=True, include_subprojects=True, include_bib=True)
    )
    assert full.references == 2 and full.pdfs == 2 and full.supplements == 1
    write_zip(full, tmp_path / "thesis.zip")
    assert _names(tmp_path / "thesis.zip") == [
        "thesis/Anand_2023.pdf", "thesis/Anand_2023_S1.csv", "thesis/Ogden_1972.pdf", "thesis/thesis.bib",
    ]  # fmt: skip
    with zipfile.ZipFile(tmp_path / "thesis.zip") as archive:
        assert archive.getinfo("thesis/Anand_2023_S1.csv").compress_type == zipfile.ZIP_DEFLATED
        assert archive.read("thesis/Anand_2023_S1.csv") == b"t,u\n0,0\n"
    assert library.get_paper(nested).citation_key == "Ogden_1972"


def test_cancel_and_vanished_files_leave_no_partial_archive(
    library: Library, make_pdf: Callable[..., Path], tmp_path: Path
) -> None:
    project = library.create_collection("Draft")
    first = _ingest(library, make_pdf, "Anand", year=2023, folder=project.id)
    _ingest(library, make_pdf, "Ogden", year=1972, folder=project.id)
    plan = library.plan_project_zip(project.id)

    target = tmp_path / "draft.zip"
    target.write_bytes(b"previous archive")
    cancel = threading.Event()
    report = write_zip(plan, target, progress=lambda *_: cancel.set(), cancel=cancel)
    assert report.cancelled
    assert target.read_bytes() == b"previous archive"  # untouched
    assert not (tmp_path / "draft.zip.part").exists()

    library.pdf_path(library.get_paper(first)).unlink()  # deleted after the plan was made
    report = write_zip(plan, target)
    assert report.pdfs == 1 and len(report.skipped) == 1 and report.skipped[0].startswith("Anand_2023.pdf:")
    assert _names(target) == ["draft/Ogden_1972.pdf", "draft/draft.bib"]

    with pytest.raises(OSError):
        write_zip(plan, tmp_path / "no such folder" / "draft.zip")
    assert not (tmp_path / "no such folder").exists()


def test_unique_arcnames_ignore_case() -> None:
    taken: set[str] = set()
    names = [unique_arcname(taken, n) for n in ("p/A_2020.pdf", "p/a_2020.pdf", "p/A_2020.pdf", "p/README")]
    assert names == ["p/A_2020.pdf", "p/a_2020-2.pdf", "p/A_2020-3.pdf", "p/README"]
