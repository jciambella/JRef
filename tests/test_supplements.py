"""v0.7: several files per reference -- the main PDF plus supplementary files (SI, data, videos, ...)."""

from __future__ import annotations

import shutil
from collections.abc import Callable
from pathlib import Path

import pytest

from bibmanager.core import Author, IngestStatus, Library, LibraryError, PaperMetadata
from bibmanager.core.mendeley import MendeleyImportOptions, plan_import
from bibmanager.core.query import build_record, matches, parse_query

from .test_import import MENDELEY_BIB, write_pdf
from .test_sync import Devices, devices, mirror, phone_config, snapshot  # noqa: F401  (fixture)


def meta(*surnames: str, year: int | None = 2021, title: str = "Gels under swelling") -> PaperMetadata:
    return PaperMetadata(title=title, authors=[Author(family=s, given="J.") for s in surnames], year=year)


def files_under(root: Path) -> list[str]:
    return sorted(p.relative_to(root).as_posix() for p in (root / "pdfs").rglob("*") if p.is_file())


@pytest.fixture
def paper_with_pdf(library: Library, make_pdf: Callable[..., Path]) -> int:
    return library.ingest_pdf(make_pdf("main.pdf", lines=["main body text"]), metadata=meta("Anand")).paper.id


def test_supplementary_files_are_stored_next_to_the_main_pdf(
    library: Library, paper_with_pdf: int, make_pdf: Callable[..., Path], tmp_path: Path
) -> None:
    si = make_pdf("mmc1.pdf", lines=["supplementary derivation of the Jacobian"])
    data = tmp_path / "Data S1.xlsx"
    data.write_bytes(b"PK\x03\x04 fake spreadsheet")
    first = library.add_supplement(paper_with_pdf, library.prepare_attachment(si), mode="copy")
    second = library.add_supplement(paper_with_pdf, library.prepare_attachment(data), mode="move")

    assert (first.relative_path, first.position, first.label, first.is_pdf) == (
        "pdfs/2021/Anand_2021_S1.pdf", 1, "mmc1", True)  # fmt: skip
    assert (second.relative_path, second.label, second.is_pdf) == ("pdfs/2021/Anand_2021_S2.xlsx", "Data S1", False)
    assert si.is_file() and not data.exists()  # copy keeps the source, move takes it
    assert files_under(library.root) == [
        "pdfs/2021/Anand_2021.pdf", "pdfs/2021/Anand_2021_S1.pdf", "pdfs/2021/Anand_2021_S2.xlsx"]  # fmt: skip
    paper = library.get_paper(paper_with_pdf)
    assert paper.attachments == 2 and library.papers()[0].attachments == 2
    assert [a.display_name for a in library.attachments(paper.id)] == ["mmc1", "Data S1"]
    assert all(library.attachment_available(a) for a in library.attachments(paper.id))
    # the text of a supplementary PDF is searchable, and has:supp finds the paper
    assert [h.paper_id for h in library.search_fulltext("Jacobian")] == [paper.id]
    assert matches(parse_query("has:supp"), build_record(paper)) and matches(parse_query("has:si"), build_record(paper))
    assert library.rename_supplement(second.id, "  Raw   data ").label == "Raw data"


def test_the_same_file_cannot_be_in_the_library_twice(
    library: Library, paper_with_pdf: int, make_pdf: Callable[..., Path]
) -> None:
    si = make_pdf("si.pdf")
    library.add_supplement(paper_with_pdf, library.prepare_attachment(si), mode="copy")
    with pytest.raises(LibraryError, match="already in the library .this document"):
        library.add_supplement(paper_with_pdf, library.prepare_attachment(si), mode="copy")
    other = library.ingest_pdf(make_pdf("other.pdf"), metadata=meta("Boyce", title="Other")).paper
    main = library.pdf_path(library.get_paper(paper_with_pdf))
    assert main is not None
    with pytest.raises(LibraryError, match="Anand_2021"):
        library.add_supplement(other.id, library.prepare_attachment(main), mode="copy")
    # importing a supplementary file as a new document finds the paper it belongs to
    result = library.ingest_pdf(si)
    assert result.status is IngestStatus.DUPLICATE_HASH and result.paper.id == paper_with_pdf
    assert library.known_sha256(library.prepare_attachment(si).sha256)


def test_supplementary_files_follow_key_and_year_and_go_with_the_paper(
    library: Library, paper_with_pdf: int, make_pdf: Callable[..., Path]
) -> None:
    for name in ("a.pdf", "b.pdf"):
        library.add_supplement(paper_with_pdf, library.prepare_attachment(make_pdf(name)), mode="copy")
    library.update_metadata(paper_with_pdf, authors="Holzapfel, G.; Ogden, R.", year=2000)
    assert files_under(library.root) == [
        "pdfs/2000/Holzapfel_Ogden_2000.pdf", "pdfs/2000/Holzapfel_Ogden_2000_S1.pdf",
        "pdfs/2000/Holzapfel_Ogden_2000_S2.pdf"]  # fmt: skip
    assert [a.relative_path for a in library.attachments(paper_with_pdf)] == [
        "pdfs/2000/Holzapfel_Ogden_2000_S1.pdf", "pdfs/2000/Holzapfel_Ogden_2000_S2.pdf"]  # fmt: skip
    library.update_metadata(paper_with_pdf, citation_key="HolzapfelOgden2000")  # an explicit key edit
    assert files_under(library.root)[1] == "pdfs/2000/HolzapfelOgden2000_S1.pdf"

    first = library.attachments(paper_with_pdf)[0]
    library.remove_supplement(first.id)
    assert files_under(library.root) == ["pdfs/2000/HolzapfelOgden2000.pdf", "pdfs/2000/HolzapfelOgden2000_S2.pdf"]
    library.delete_paper(paper_with_pdf)
    assert files_under(library.root) == []


def test_removing_a_supplement_and_rebuilding_the_full_text(
    library: Library, paper_with_pdf: int, make_pdf: Callable[..., Path]
) -> None:
    supp = library.add_supplement(
        paper_with_pdf, library.prepare_attachment(make_pdf("s.pdf", lines=["Mooney calibration table"])), mode="copy"
    )
    assert library.search_fulltext("calibration")
    library.remove_supplement(supp.id)
    assert library.search_fulltext("calibration")  # until the entry is rebuilt (the GUI does it in a worker)
    library.set_fulltext(paper_with_pdf, library.read_fulltext(library.fulltext_sources(paper_with_pdf)))
    assert not library.search_fulltext("calibration") and library.search_fulltext("main body text")


def test_linked_supplements_are_never_moved_or_deleted(
    library: Library, paper_with_pdf: int, tmp_path: Path
) -> None:
    outside = write_pdf(tmp_path / "Mendeley" / "Anand - 2021 - SI.pdf", "si")
    supp = library.add_supplement(paper_with_pdf, library.prepare_attachment(outside), mode="link")
    assert supp.linked and Path(supp.relative_path) == outside and library.attachment_path(supp) == outside
    library.update_metadata(paper_with_pdf, authors="Gent, A.")
    assert library.attachments(paper_with_pdf)[0].relative_path == supp.relative_path
    library.delete_paper(paper_with_pdf)
    assert outside.is_file()


def test_promoting_a_supplementary_pdf_to_main_swaps_the_files(
    library: Library, paper_with_pdf: int, make_pdf: Callable[..., Path], tmp_path: Path
) -> None:
    root = library.root
    main_before = library.pdf_path(library.get_paper(paper_with_pdf)).read_bytes()  # type: ignore[union-attr]
    supp = library.add_supplement(paper_with_pdf, library.prepare_attachment(make_pdf("real.pdf")), mode="copy")
    supp_before = library.attachment_path(supp).read_bytes()
    data = tmp_path / "table.csv"
    data.write_text("a,b\n1,2\n")
    csv = library.add_supplement(paper_with_pdf, library.prepare_attachment(data), mode="copy")
    with pytest.raises(LibraryError, match="Only a PDF"):
        library.promote_supplement(csv.id)

    paper = library.promote_supplement(supp.id)
    assert paper.relative_pdf_path == "pdfs/2021/Anand_2021.pdf" and paper.sha256 == supp.sha256
    assert (root / "pdfs/2021/Anand_2021.pdf").read_bytes() == supp_before
    former = library.get_attachment(supp.id)
    assert (former.relative_path, former.label) == ("pdfs/2021/Anand_2021_S1.pdf", "Former main PDF")
    assert (root / former.relative_path).read_bytes() == main_before
    assert files_under(root) == ["pdfs/2021/Anand_2021.pdf", "pdfs/2021/Anand_2021_S1.pdf", "pdfs/2021/Anand_2021_S2.csv"]

    # a reference without PDF: its supplementary PDF simply becomes the main PDF
    ref = library.add_reference(meta("Treloar", year=1944, title="Rubber")).paper
    only = library.add_supplement(ref.id, library.prepare_attachment(make_pdf("t.pdf")), mode="copy")
    promoted = library.promote_supplement(only.id)
    assert promoted.relative_pdf_path == "pdfs/1944/Treloar_1944.pdf" and promoted.attachments == 0
    assert library.pdf_available(promoted)


def test_mendeley_entries_with_several_files_get_supplements(library: Library, tmp_path: Path) -> None:
    folder = tmp_path / "Mendeley Desktop"
    write_pdf(folder / "Hydrogels" / "Ciambella et al. - 2025 - Hydrogel fracture.pdf", "hydrogel")
    (folder / "supp_data.zip").write_bytes(b"PK\x05\x06" + bytes(18))
    bib = tmp_path / "library.bib"
    bib.write_text(MENDELEY_BIB, encoding="utf-8")
    options = MendeleyImportOptions(bib_paths=[bib], pdf_folder=folder)

    def run() -> list[int]:
        counts = []
        with library.bulk_update():
            for cand in plan_import(options).candidates:
                cand = library.prepare_reference(cand)
                counts.append(library.commit_reference(cand, file_mode=options.file_mode).supplements)
        return counts

    assert sum(run()) == 1
    paper = library.db.get_paper_by_citation_key("Ciambella2025")
    assert paper is not None and paper.attachments == 1
    (zipped,) = library.attachments(paper.id)
    assert zipped.linked and library.attachment_path(zipped) == folder / "supp_data.zip" and zipped.label == "supp_data"
    assert sum(run()) == 0  # re-import: nothing twice
    assert library.get_paper(paper.id).attachments == 1


# --------------------------------------------------------------------------- shared library
def test_supplements_travel_between_devices_and_follow_renames(
    tmp_path: Path, devices: Devices, make_pdf: Callable[..., Path]
) -> None:
    root = tmp_path / "Library"
    desk = devices.library("desk", root)
    paper = desk.ingest_pdf(make_pdf("main.pdf"), metadata=meta("Smith", year=2020)).paper
    phone = devices.library("phone", root, **phone_config(devices, root))
    for lib in (desk, phone, desk):
        lib.sync_now()
    on_phone = phone.db.get_paper_by_citation_key("Smith_2020")
    supp = phone.add_supplement(on_phone.id, phone.prepare_attachment(make_pdf("si.pdf")), mode="copy")
    phone.sync_now()
    desk.sync_now()
    (received,) = desk.attachments(paper.id)
    assert received.uid == supp.uid and received.relative_path == "pdfs/2020/Smith_2020_S1.pdf"
    assert desk.attachment_available(received) and desk.get_paper(paper.id).attachments == 1

    desk.update_metadata(paper.id, authors="Jones, A.")  # the desktop renames key, PDF and supplement
    desk.rename_supplement(received.id, "Supplementary Information")
    desk.sync_now()
    phone.sync_now()
    (moved,) = phone.attachments(on_phone.id)
    assert (moved.relative_path, moved.label) == ("pdfs/2020/Jones_2020_S1.pdf", "Supplementary Information")
    assert phone.attachment_available(moved)
    phone.remove_supplement(moved.id)
    phone.sync_now()
    desk.sync_now()
    assert desk.attachments(paper.id) == [] and not (root / "pdfs/2020/Jones_2020_S1.pdf").exists()


def test_supplements_added_on_two_devices_offline_get_distinct_numbers(
    tmp_path: Path, devices: Devices, make_pdf: Callable[..., Path]
) -> None:
    root_a, root_b = tmp_path / "A" / "Library", tmp_path / "B" / "Library"
    desk = devices.library("desk", root_a)
    desk.ingest_pdf(make_pdf("main.pdf"), metadata=meta("Gent", year=1996))
    desk.sync_now()
    mirror(root_a, root_b)
    phone = devices.library("phone", root_b, **phone_config(devices, root_b))
    phone.sync_now()
    a = desk.add_supplement(desk.papers()[0].id, desk.prepare_attachment(make_pdf("one.pdf")), mode="copy")
    b = phone.add_supplement(phone.papers()[0].id, phone.prepare_attachment(make_pdf("two.pdf")), mode="copy")
    assert a.relative_path == b.relative_path == "pdfs/1996/Gent_1996_S1.pdf"
    for _ in range(3):
        desk.sync_now()
        phone.sync_now()
        mirror(root_a, root_b)

    assert snapshot(desk) == snapshot(phone)
    paths = {lib: sorted((x.uid, x.relative_path) for x in lib.attachments(lib.papers()[0].id)) for lib in (desk, phone)}
    assert paths[desk] == paths[phone]
    assert sorted(path for _uid, path in paths[desk]) == ["pdfs/1996/Gent_1996_S1.pdf", "pdfs/1996/Gent_1996_S2.pdf"]
    for lib, own in ((desk, a), (phone, b)):  # each device's own file is under its final name there
        mine = next(x for x in lib.attachments(lib.papers()[0].id) if x.uid == own.uid)
        assert lib.attachment_path(mine).read_bytes() == own_bytes(own, lib, tmp_path)


def own_bytes(attachment: object, lib: Library, tmp_path: Path) -> bytes:
    """The bytes of the file a device attached itself (identified by its hash)."""
    import hashlib

    for path in (tmp_path / "downloads").iterdir():
        if hashlib.sha256(path.read_bytes()).hexdigest() == attachment.sha256:  # type: ignore[attr-defined]
            return path.read_bytes()
    raise AssertionError("source not found")


def test_a_library_of_an_earlier_release_opens_with_no_supplements(tmp_path: Path, library: Library) -> None:
    assert library.db.conn.execute("PRAGMA user_version").fetchone()[0] == 5
    copy = tmp_path / "copy"
    shutil.copytree(library.root, copy)
    assert all(p.attachments == 0 for p in library.papers())
