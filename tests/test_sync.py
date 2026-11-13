"""Sharing one library between devices (``bibmanager.core.sync``): desktop and phone on one synced folder.

Two set-ups are exercised:

* both "devices" open the *same* folder (an idealised sync client that is always up to date);
* each device has its *own copy* of the folder and a toy sync client (``mirror``) copies new files across,
  as Dropbox or Syncthing would. Nothing but the files in the folder is shared.

Every device has its own data folder (database, device id), like two machines would.
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from bibmanager.core import AppConfig, Author, IngestStatus, Library, MetadataResolver, PaperMetadata
from bibmanager.core import extractor
from bibmanager.core import sync as device_sync

from .conftest import CONFIG_PATH


def meta(*surnames: str, year: int | None, title: str = "On the mechanics of soft solids", **kw: Any) -> PaperMetadata:
    return PaperMetadata(title=title, authors=[Author(family=s, given="J.") for s in surnames], year=year, **kw)


def _no_network(request: httpx.Request) -> httpx.Response:
    raise AssertionError(f"Unexpected network access during tests: {request.url}")


class Devices:
    """Opens libraries as different devices and closes them at the end of the test."""

    def __init__(self, tmp_path: Path) -> None:
        self.tmp = tmp_path
        self.open: list[Library] = []
        self.client = httpx.Client(transport=httpx.MockTransport(_no_network))

    def config(self, name: str, root: Path, **overrides: Any) -> AppConfig:
        settings: dict[str, Any] = {
            "library_root": root,
            "import_mode": "copy",
            "sync_enabled": True,
            "device_name": name,
            "local_data_dir": self.tmp / f"data-{name}",
        }
        return AppConfig.load(CONFIG_PATH, **(settings | overrides))

    def library(self, name: str, root: Path, **overrides: Any) -> Library:
        config = self.config(name, root, **overrides)
        lib = Library(config, resolver=MetadataResolver.from_config(config, client=self.client))
        self.open.append(lib)
        return lib

    def close(self) -> None:
        for lib in self.open:
            try:
                lib.close()
            except Exception:  # already closed by the test
                pass
        self.client.close()


@pytest.fixture
def devices(tmp_path: Path) -> Iterator[Devices]:
    d = Devices(tmp_path)
    yield d
    d.close()


def phone_config(devices: Devices, root: Path) -> dict[str, Any]:
    return {"write_bib_files": False, "adopt_folder_database": False}


def snapshot(lib: Library) -> dict[str, Any]:
    """Device-independent content of a library (no local ids)."""
    folders = {c.id: c.name for c in lib.collections()}
    papers = {}
    for p in lib.papers():
        papers[p.citation_key] = (
            p.meta.title,
            p.meta.authors_text(),
            p.meta.year,
            p.meta.doi,
            p.relative_pdf_path,
            p.sha256,
            p.favorite,
            p.is_read,
            p.tags,
            p.notes,
            tuple(sorted(c.name for c in lib.collections_of(p.id))),
        )
    return {"papers": papers, "folders": sorted(folders.values())}


def sync_all(*libs: Library, rounds: int = 3) -> None:
    for _ in range(rounds):
        for lib in libs:
            lib.sync_now()


def mirror(a: Path, b: Path) -> None:
    """Toy sync client: copy files that exist on one side only, and longer versions of append-only journals."""
    for source, target in ((a, b), (b, a)):
        for path in sorted(source.rglob("*")):
            relative = path.relative_to(source)
            if path.is_dir() or relative.name.endswith(".tmp"):
                continue
            other = target / relative
            if not other.exists() or (relative.suffix == ".jsonl" and other.stat().st_size < path.stat().st_size):
                other.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, other)


# =========================================================================== 1. enabling, bootstrap
def test_sharing_moves_the_database_out_of_the_folder_and_a_second_device_gets_everything(
    tmp_path: Path, devices: Devices, make_pdf: Callable[..., Path]
) -> None:
    root = tmp_path / "Dropbox" / "Library"
    plain = AppConfig.load(CONFIG_PATH, library_root=root, import_mode="copy", local_data_dir=tmp_path / "data-desk")
    with Library(plain, resolver=MetadataResolver.from_config(plain, client=devices.client)) as lib:
        thesis = lib.create_collection("Thesis")
        first = lib.ingest_pdf(make_pdf("a.pdf"), thesis.id, metadata=meta("Anand", year=2023)).paper
        lib.ingest_pdf(make_pdf("b.pdf"), metadata=meta("Ciambella", "Lancioni", year=2025))
        lib.update_flags(first.id, favorite=True, tags="fracture; review", notes="Read section 3")
        assert (root / "library.db").is_file() and not lib.shared
        first_id = first.id

    desk = devices.library("desk", root)
    assert desk.shared and not (root / "library.db").exists()  # nothing but sync-safe files in the folder
    assert desk.db_path.is_relative_to(tmp_path / "data-desk")
    assert desk.get_paper(first_id).citation_key == "Anand_2023"  # the database was moved, ids kept
    report = desk.sync_now()
    assert report.pushed >= 4  # 1 folder + 2 papers + 1 membership
    journals = list((root / "sync").glob("*/*.jsonl"))
    assert len(journals) == 1 and journals[0].parent.name == desk.sync.device_id

    phone = devices.library("phone", root, **phone_config(devices, root))
    assert phone.db_path != desk.db_path and phone.papers() == []
    report = phone.sync_now()
    assert report.papers_added == 2 and report.devices == 1
    assert snapshot(phone) == snapshot(desk)
    paper = phone.db.get_paper_by_citation_key("Anand_2023")
    assert paper is not None and paper.favorite and paper.notes == "Read section 3"
    assert phone.pdf_available(paper) and phone.pdf_path(paper) == desk.pdf_path(first)
    assert phone.search_fulltext("mechanics")  # metadata is indexed on the receiving device too
    assert phone.sync_now().summary() == "Up to date"
    assert [d.name for d in desk.sync.devices()] == ["desk", "phone"]


def test_paper_added_on_the_phone_reaches_the_desktop_with_its_folder_and_bib(
    tmp_path: Path, devices: Devices, make_pdf: Callable[..., Path]
) -> None:
    root = tmp_path / "Library"
    desk = devices.library("desk", root)
    desk.create_collection("Thesis")
    desk.sync_now()
    phone = devices.library("phone", root, **phone_config(devices, root))
    phone.sync_now()
    folder = next(c for c in phone.collections() if c.name == "Thesis")

    result = phone.ingest_pdf(make_pdf("download.pdf"), folder.id, metadata=meta("Ogden", year=1997, doi="10.5555/x1"))
    assert result.status is IngestStatus.IMPORTED
    assert phone.push_changes() == 2  # the paper and its folder link
    assert not list((root / "bibtex").glob("*.bib")) or "Ogden_1997" not in (root / "bibtex" / "thesis.bib").read_text()

    report = desk.sync_now()
    assert report.papers_added == 1 and report.summary() == "2 received, 1 new"
    paper = desk.db.get_paper_by_citation_key("Ogden_1997")
    assert paper is not None and paper.meta.doi == "10.5555/x1"
    assert desk.pdf_path(paper) == root / "pdfs" / "1997" / "Ogden_1997.pdf" and desk.pdf_available(paper)
    assert [c.name for c in desk.collections_of(paper.id)] == ["Thesis"]
    assert "@article{Ogden_1997," in (root / "bibtex" / "thesis.bib").read_text(encoding="utf-8")
    assert "@article{Ogden_1997," in (root / "bibtex" / "master.bib").read_text(encoding="utf-8")


# =========================================================================== 2. merging
def test_offline_edits_of_different_fields_merge_and_the_later_edit_of_one_field_wins(
    tmp_path: Path, devices: Devices, make_pdf: Callable[..., Path]
) -> None:
    root = tmp_path / "Library"
    desk = devices.library("desk", root)
    paper = desk.ingest_pdf(make_pdf(), metadata=meta("Rivlin", year=1948, title="Large elastic deformations")).paper
    phone = devices.library("phone", root, **phone_config(devices, root))
    sync_all(desk, phone)
    on_phone = phone.db.get_paper_by_citation_key("Rivlin_1948")
    assert on_phone is not None

    # "Offline": both edit before either synchronises.
    desk.update_flags(paper.id, favorite=True, tags="elasticity")
    desk.update_metadata(paper.id, journal="Phil. Trans.")
    desk.push_changes()
    phone.update_flags(on_phone.id, notes="check eq. 12", is_read=True)
    phone.update_metadata(on_phone.id, journal="Philosophical Transactions A")  # later than the desktop's edit
    phone.push_changes()
    sync_all(desk, phone)

    for lib in (desk, phone):
        merged = lib.db.get_paper_by_citation_key("Rivlin_1948")
        assert merged is not None
        assert merged.favorite and merged.tags == "elasticity"  # from the desktop
        assert merged.notes == "check eq. 12" and merged.is_read  # from the phone
        assert merged.meta.journal == "Philosophical Transactions A"  # last writer wins
    assert snapshot(desk) == snapshot(phone)


def test_delete_wins_over_an_edit_and_removes_folder_links(
    tmp_path: Path, devices: Devices, make_pdf: Callable[..., Path]
) -> None:
    root = tmp_path / "Library"
    desk = devices.library("desk", root)
    folder = desk.create_collection("Reading")
    doomed = desk.ingest_pdf(make_pdf("a.pdf"), folder.id, metadata=meta("Hill", year=1950)).paper
    kept = desk.ingest_pdf(make_pdf("b.pdf"), folder.id, metadata=meta("Mandel", year=1971)).paper
    phone = devices.library("phone", root, **phone_config(devices, root))
    sync_all(desk, phone)

    desk.delete_paper(doomed.id)
    desk.remove_from_collection(kept.id, folder.id)
    desk.push_changes()
    on_phone = phone.db.get_paper_by_citation_key("Hill_1950")
    assert on_phone is not None
    phone.update_flags(on_phone.id, notes="edited after the deletion, before hearing of it")
    sync_all(desk, phone)

    for lib in (desk, phone):
        assert sorted(p.citation_key for p in lib.papers()) == ["Mandel_1971"]
        assert lib.papers(next(c.id for c in lib.collections())) == []
    assert not (root / "pdfs" / "1950" / "Hill_1950.pdf").exists()  # deleted by the device that deleted the entry


def test_the_same_pdf_added_on_two_devices_is_kept_twice_and_reported(
    tmp_path: Path, devices: Devices, make_pdf: Callable[..., Path]
) -> None:
    """Nothing is merged or deleted automatically (no such rule is safe when changes arrive in any order):
    the second copy gets the next key suffix on both devices and the user is told about it."""
    root_a, root_b = tmp_path / "A" / "Library", tmp_path / "B" / "Library"
    desk = devices.library("desk", root_a)
    desk.create_collection("Thesis")
    desk.sync_now()
    mirror(root_a, root_b)
    phone = devices.library("phone", root_b, **phone_config(devices, root_b))
    phone.sync_now()
    phone.create_collection("To read")

    pdf = make_pdf("same.pdf")
    twin = tmp_path / "copy-on-phone.pdf"
    shutil.copy2(pdf, twin)
    info = meta("Gent", year=1996, doi="10.5555/gent")
    a = desk.ingest_pdf(pdf, desk.collections()[0].id, metadata=info)
    b = phone.ingest_pdf(twin, next(c.id for c in phone.collections() if c.name == "To read"), metadata=info)
    assert a.paper.citation_key == b.paper.citation_key == "Gent_1996"
    reported = []
    for _ in range(3):
        reported += desk.sync_now().duplicates + phone.sync_now().duplicates
        mirror(root_a, root_b)

    assert snapshot(desk) == snapshot(phone)
    assert sorted(map(sorted, reported)) == [["Gent_1996", "Gent_1996a"]] * 2  # each device noticed it once
    for lib in (desk, phone):
        assert sorted(p.citation_key for p in lib.papers()) == ["Gent_1996", "Gent_1996a"]
        assert all(lib.pdf_available(p) for p in lib.papers())
        folders = sorted(c.name for p in lib.papers() for c in lib.collections_of(p.id))
        assert folders == ["Thesis", "To read"]
    # A duplicate is still refused on one device, and deleting one copy leaves the other everywhere.
    third = tmp_path / "third.pdf"
    shutil.copy2(pdf, third)
    assert desk.ingest_pdf(third).status is IngestStatus.DUPLICATE_HASH
    desk.delete_paper(desk.db.get_paper_by_citation_key("Gent_1996a").id)
    desk.sync_now()
    mirror(root_a, root_b)
    phone.sync_now()
    assert [p.citation_key for p in phone.papers()] == ["Gent_1996"]


def test_two_different_papers_with_the_same_key_get_distinct_keys_on_both_devices(
    tmp_path: Path, devices: Devices, make_pdf: Callable[..., Path]
) -> None:
    root_a, root_b = tmp_path / "A" / "Library", tmp_path / "B" / "Library"
    desk = devices.library("desk", root_a)
    desk.sync_now()
    mirror(root_a, root_b)
    phone = devices.library("phone", root_b, **phone_config(devices, root_b))
    phone.sync_now()

    one = desk.ingest_pdf(make_pdf("one.pdf"), metadata=meta("Simo", year=1987, title="First paper")).paper
    two = phone.ingest_pdf(make_pdf("two.pdf"), metadata=meta("Simo", year=1987, title="Second paper")).paper
    assert one.citation_key == two.citation_key == "Simo_1987"
    desk.sync_now()
    phone.sync_now()
    # The sync client cannot put two different files under one name: it keeps each side's own file. JRef renames
    # the entry that gives way on the device that owns that file, after which the files no longer collide.
    for _ in range(3):
        desk.sync_now()
        phone.sync_now()
        mirror(root_a, root_b)

    assert snapshot(desk) == snapshot(phone)
    keys = {p.meta.title: p.citation_key for p in desk.papers()}
    assert sorted(keys.values()) == ["Simo_1987", "Simo_1987a"]
    for lib, own_title in ((desk, "First paper"), (phone, "Second paper")):
        own = next(p for p in lib.papers() if p.meta.title == own_title)
        assert lib.pdf_available(own)
        assert lib.pdf_path(own).name == f"{own.citation_key}.pdf"


def test_renaming_after_an_author_edit_follows_on_the_other_device(
    tmp_path: Path, devices: Devices, make_pdf: Callable[..., Path]
) -> None:
    root = tmp_path / "Library"
    desk = devices.library("desk", root)
    folder = desk.create_collection("Thesis")
    paper = desk.ingest_pdf(make_pdf(), folder.id, metadata=meta("Smith", year=2020)).paper
    phone = devices.library("phone", root, **phone_config(devices, root))
    sync_all(desk, phone)

    on_phone = phone.db.get_paper_by_citation_key("Smith_2020")
    phone.update_metadata(on_phone.id, authors="Holzapfel, G.; Gasser, T.", year=2021)
    sync_all(phone, desk)

    renamed = desk.get_paper(paper.id)
    assert renamed.citation_key == "Holzapfel_Gasser_2021"
    assert renamed.relative_pdf_path == "pdfs/2021/Holzapfel_Gasser_2021.pdf" and desk.pdf_available(renamed)
    bib = (root / "bibtex" / "thesis.bib").read_text(encoding="utf-8")
    assert "@article{Holzapfel_Gasser_2021," in bib and "Smith_2020" not in bib


def test_folders_created_with_the_same_name_on_two_devices_are_one_folder_and_nesting_survives(
    tmp_path: Path, devices: Devices, make_pdf: Callable[..., Path]
) -> None:
    root = tmp_path / "Library"
    desk = devices.library("desk", root)
    phone = devices.library("phone", root, **phone_config(devices, root))
    sync_all(desk, phone)
    a = desk.create_collection("Viscoelastic fracture")
    desk.create_collection("Chapter 2", parent_id=a.id)
    b = phone.create_collection("Viscoelastic fracture")
    paper = phone.ingest_pdf(make_pdf(), b.id, metadata=meta("Knauss", year=1970)).paper
    sync_all(desk, phone)

    for lib in (desk, phone):
        names = sorted(c.name for c in lib.collections())
        assert names == ["Chapter 2", "Viscoelastic fracture"]
        parent = next(c for c in lib.collections() if c.name == "Viscoelastic fracture")
        assert next(c for c in lib.collections() if c.name == "Chapter 2").parent_id == parent.id
        assert [p.citation_key for p in lib.papers(parent.id)] == ["Knauss_1970"]

    desk.rename_collection(a.id, "Fracture")
    desk.delete_collection(next(c.id for c in desk.collections() if c.name == "Chapter 2"))
    sync_all(desk, phone)
    assert sorted(c.name for c in phone.collections()) == ["Fracture"]
    assert [c.name for c in phone.collections_of(paper.id)] == ["Fracture"]
    assert (root / "bibtex" / "fracture.bib").is_file() and not (root / "bibtex" / "viscoelastic_fracture.bib").exists()


# =========================================================================== 3. robustness of the journal
def test_a_third_device_replays_the_whole_history_and_a_half_transferred_line_waits(
    tmp_path: Path, devices: Devices, make_pdf: Callable[..., Path]
) -> None:
    root = tmp_path / "Library"
    desk = devices.library("desk", root)
    phone = devices.library("phone", root, **phone_config(devices, root))
    folder = desk.create_collection("Thesis")
    for index in range(6):
        desk.ingest_pdf(make_pdf(f"p{index}.pdf"), folder.id, metadata=meta(f"Author{index}", year=2000 + index))
    sync_all(desk, phone)
    victim = phone.db.get_paper_by_citation_key("Author3_2003")
    phone.delete_paper(victim.id)
    phone.update_flags(phone.db.get_paper_by_citation_key("Author1_2001").id, favorite=True)
    sync_all(phone, desk)

    # A sync client that has delivered only part of the last line of the desktop's journal.
    journal = next((root / "sync" / desk.sync.device_id).glob("*.jsonl"))
    complete = journal.read_bytes()
    desk.update_flags(desk.db.get_paper_by_citation_key("Author5_2005").id, tags="late")
    desk.push_changes()
    full = journal.read_bytes()
    journal.write_bytes(full[: len(complete) + 25])
    assert phone.sync_now().applied == 0
    journal.write_bytes(full)
    assert phone.sync_now().applied == 1
    assert phone.db.get_paper_by_citation_key("Author5_2005").tags == "late"

    laptop = devices.library("laptop", root)
    report = laptop.sync_now()
    assert report.papers_added == 6 and report.papers_deleted == 1 and report.waiting == 0
    assert snapshot(laptop) == snapshot(desk) == snapshot(phone)
    assert len(laptop.papers()) == 5

    # Every line of every journal is one self-contained JSON object with a clock stamp.
    for path in (root / "sync").glob("*/*.jsonl"):
        for line in path.read_text(encoding="utf-8").splitlines():
            op = json.loads(line)
            assert op["k"] in {"p", "c", "m"} and len(op["t"].split(".")) == 3


def test_changes_arriving_before_what_they_refer_to_are_applied_later(
    tmp_path: Path, devices: Devices, make_pdf: Callable[..., Path]
) -> None:
    root_a, root_b, root_c = (tmp_path / n / "Library" for n in "ABC")
    desk = devices.library("desk", root_a)
    desk.sync_now()
    mirror(root_a, root_b)
    phone = devices.library("phone", root_b, **phone_config(devices, root_b))
    phone.sync_now()
    paper = desk.ingest_pdf(make_pdf(), metadata=meta("Treloar", year=1944)).paper
    desk.sync_now()
    mirror(root_a, root_b)
    phone.sync_now()
    folder = phone.create_collection("Rubber")
    phone.add_to_collection(phone.db.get_paper_by_citation_key("Treloar_1944").id, folder.id)
    phone.update_flags(phone.db.get_paper_by_citation_key("Treloar_1944").id, favorite=True)
    phone.sync_now()

    # The tablet's sync client delivers the phone's journal first, the desktop's (with the paper itself) later.
    shutil.copytree(root_b / "sync" / phone.sync.device_id, root_c / "sync" / phone.sync.device_id)
    shutil.copy2(root_b / "sync" / "library.json", root_c / "sync" / "library.json")
    tablet = devices.library("tablet", root_c, **phone_config(devices, root_c))
    report = tablet.sync_now()
    assert tablet.papers() == [] and report.waiting == 2 and [c.name for c in tablet.collections()] == ["Rubber"]
    shutil.copytree(root_a / "sync" / desk.sync.device_id, root_c / "sync" / desk.sync.device_id)
    report = tablet.sync_now()
    assert report.waiting == 0
    (received,) = tablet.papers()
    assert received.favorite and [c.name for c in tablet.collections_of(received.id)] == ["Rubber"]
    assert not tablet.pdf_available(received)  # the PDF itself has not been delivered yet: no error, just no file
    assert paper.citation_key == received.citation_key


# =========================================================================== 4. linked PDFs, readers
def test_linked_pdfs_inside_the_library_folder_are_portable_and_others_are_mapped(
    tmp_path: Path, devices: Devices, make_pdf: Callable[..., Path]
) -> None:
    root = tmp_path / "Library"
    papers_dir = root / "Mendeley" / "2019"
    papers_dir.mkdir(parents=True)
    inside = shutil.copy2(make_pdf("in.pdf"), papers_dir / "Anand - 2019 - Title.pdf")
    outside = make_pdf("out.pdf")  # e.g. C:\Users\x\Documents\Papers on the desktop

    desk = devices.library("desk", root)
    one = desk.add_reference(meta("Anand", year=2019)).paper
    two = desk.add_reference(meta("Boyce", year=2000, title="Another title")).paper
    desk.attach_pdf(one.id, desk.prepare_attachment(Path(inside)), mode="link")
    desk.attach_pdf(two.id, desk.prepare_attachment(outside), mode="link")
    assert desk.get_paper(one.id).relative_pdf_path == "Mendeley/2019/Anand - 2019 - Title.pdf"
    assert Path(desk.get_paper(two.id).relative_pdf_path).is_absolute()
    desk.sync_now()

    phone = devices.library("phone", root, **phone_config(devices, root))
    phone.sync_now()
    first, second = (phone.db.get_paper_by_citation_key(k) for k in ("Anand_2019", "Boyce_2000"))
    assert phone.pdf_available(first) and phone.pdf_path(first) == Path(inside)
    assert phone.pdf_available(second)  # same machine in this test: the absolute path exists

    # On a real phone the desktop's absolute path means nothing: map the recorded folder to a local one.
    desk_folder = "C:\\Users\\jc\\Documents\\Papers"
    phone.db.conn.execute(
        "UPDATE papers SET relative_pdf_path = ? WHERE id = ?", (desk_folder + "\\sub\\Boyce 2000.pdf", second.id)
    )
    second = phone.get_paper(second.id)
    assert not phone.pdf_available(second)
    assert phone.unresolved_link_folders() == [("C:/Users/jc/Documents/Papers/sub", 1)]
    local = tmp_path / "phone-storage" / "Papers"
    (local / "sub").mkdir(parents=True)
    shutil.copy2(outside, local / "sub" / "Boyce 2000.pdf")
    phone.config.linked_path_map = {"c:/users/jc/documents/papers": str(local)}
    assert phone.pdf_path(second) == local / "sub" / "Boyce 2000.pdf" and phone.pdf_available(second)
    assert phone.unresolved_link_folders() == []


def test_absolute_links_inside_the_folder_are_made_relative_when_the_library_is_opened(
    tmp_path: Path, devices: Devices, make_pdf: Callable[..., Path]
) -> None:
    root = tmp_path / "Papers"  # the folder where the PDFs already are becomes the library folder
    root.mkdir()
    existing = shutil.copy2(make_pdf("x.pdf"), root / "Ogden - 1984 - Non-linear elastic deformations.pdf")
    desk = devices.library("desk", root)
    paper = desk.add_reference(meta("Ogden", year=1984)).paper
    desk.attach_pdf(paper.id, desk.prepare_attachment(Path(existing)), mode="link")
    desk.db.conn.execute("UPDATE papers SET relative_pdf_path = ? WHERE id = ?", (str(existing), paper.id))
    desk.close()

    reopened = devices.library("desk", root)
    stored = reopened.get_paper(paper.id)
    assert stored.relative_pdf_path == "Ogden - 1984 - Non-linear elastic deformations.pdf" and stored.pdf_linked
    assert reopened.pdf_path(stored) == Path(existing) and Path(existing).is_file()  # never moved or renamed


def test_reading_pdfs_without_pymupdf_and_a_database_without_fts5(
    tmp_path: Path, devices: Devices, make_pdf: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    pdf = make_pdf("doi.pdf", title="Phase-field fracture of soft solids", lines=["https://doi.org/10.5555/abc.2024.17"])
    monkeypatch.setattr(extractor, "pymupdf", None)  # as on Android: the pure-Python reader takes over
    content = extractor.read_pdf(pdf)
    assert extractor.find_dois(content.full_text) == ["10.5555/abc.2024.17"]
    with pytest.raises(extractor.PdfReadError):
        extractor.pdf_outline(pdf)

    lib = devices.library("phone", tmp_path / "Library", **phone_config(devices, tmp_path))
    lib.db.conn.executescript("DROP TRIGGER trg_papers_delete_fts; DROP TABLE papers_fts;")
    lib.db.has_fts = False  # a SQLite build without FTS5
    paper = lib.ingest_pdf(pdf, metadata=meta("Miehe", year=2024, title="Phase-field fracture")).paper
    lib.update_metadata(paper.id, journal="CMAME")
    assert [h.paper_id for h in lib.search_fulltext("phase-field")] == [paper.id]
    lib.delete_paper(paper.id)
    assert lib.papers() == []


def test_device_identity_is_stable_and_the_library_id_is_shared(tmp_path: Path) -> None:
    first = device_sync.device_identity(tmp_path / "data", "Pixel")
    assert device_sync.device_identity(tmp_path / "data") == first and first[1] == "Pixel"
    root = tmp_path / "Library"
    assert not device_sync.is_shared(root)
    assert device_sync.library_id(root) == device_sync.library_id(root) and device_sync.is_shared(root)


# =========================================================================== 5. convergence under random use
@pytest.mark.parametrize("seed", range(int(__import__("os").environ.get("JREF_SYNC_SEEDS", "12"))))
def test_three_devices_converge_whatever_the_order_of_edits_and_deliveries(
    tmp_path: Path, devices: Devices, seed: int
) -> None:
    """Random edits on three devices with a sync client that delivers files in random order and at random
    times: once everything has been delivered, the three libraries are identical."""
    import random

    rng = random.Random(seed)
    roots = [tmp_path / name / "Library" for name in "ABC"]
    libs = [devices.library(f"dev{i}", root, write_bib_files=(i == 0)) for i, root in enumerate(roots)]
    surnames = ["Anand", "Boyce", "Gent", "Ogden", "Simo"]
    folder_names = ["Thesis", "Review", "To read", "Fracture"]

    def deliver(everything: bool = False) -> None:
        pairs = [(0, 1), (1, 2), (0, 2)]
        rng.shuffle(pairs)
        for a, b in pairs:
            if everything or rng.random() < 0.6:
                mirror(roots[a], roots[b])

    def act(lib: Library) -> None:
        papers, folders = lib.papers(), lib.collections()
        choice = rng.random()
        if choice < 0.30 or not papers:
            info = meta(rng.choice(surnames), year=rng.choice([2019, 2020]), title=f"Title {rng.randrange(10**6)}")
            if rng.random() < 0.4:
                info.doi = f"10.5555/shared.{rng.randrange(4)}"  # the same DOI may be added on two devices
            folder = rng.choice(folders).id if folders and rng.random() < 0.5 else None
            lib.add_reference(info, folder)
        elif choice < 0.45:
            paper = rng.choice(papers)
            lib.update_flags(paper.id, favorite=rng.random() < 0.5, tags=rng.choice(["a", "b; c", ""]),
                             notes=f"note {rng.randrange(100)}")  # fmt: skip
        elif choice < 0.60:
            paper = rng.choice(papers)
            try:
                lib.update_metadata(paper.id, journal=f"Journal {rng.randrange(5)}", year=rng.choice([2019, 2020, 2021]))
            except Exception:
                pass
        elif choice < 0.68:
            lib.delete_paper(rng.choice(papers).id)
        elif choice < 0.78:
            lib.create_collection(rng.choice(folder_names))
        elif choice < 0.83 and folders:
            lib.rename_collection(rng.choice(folders).id, rng.choice(folder_names) + rng.choice(["", " II"]))
        elif choice < 0.87 and folders:
            lib.delete_collection(rng.choice(folders).id)
        elif choice < 0.93:  # supplementary files: add, relabel, remove
            paper = rng.choice(papers)
            existing = lib.attachments(paper.id)
            if existing and rng.random() < 0.4:
                target = rng.choice(existing)
                if rng.random() < 0.5:
                    lib.rename_supplement(target.id, f"label {rng.randrange(100)}")
                else:
                    lib.remove_supplement(target.id)
            else:
                source = tmp_path / "supp" / f"s{rng.randrange(10**9)}.dat"
                source.parent.mkdir(exist_ok=True)
                source.write_bytes(source.name.encode())
                lib.add_supplement(paper.id, lib.prepare_attachment(source), mode="copy")
        elif folders:
            paper, folder = rng.choice(papers), rng.choice(folders)
            if rng.random() < 0.7:
                lib.add_to_collection(paper.id, folder.id)
            else:
                lib.remove_from_collection(paper.id, folder.id)

    for _ in range(60):
        lib = rng.choice(libs)
        act(lib)
        if rng.random() < 0.5:
            lib.sync_now()
        if rng.random() < 0.3:
            deliver()
    for _ in range(4):  # everything is delivered and every device synchronises
        for lib in libs:
            lib.sync_now()
        deliver(everything=True)
    for lib in libs:
        lib.sync_now()

    reference = snapshot(libs[0])
    for lib in libs[1:]:
        assert snapshot(lib) == reference
    supplements = [sorted((a.uid, a.relative_path, a.label) for p in lib.papers() for a in lib.attachments(p.id)) for lib in libs]
    assert supplements[0] == supplements[1] == supplements[2]
    for lib in libs:
        paths = [path.casefold() for _uid, path, _label in supplements[0]]
        assert len(paths) == len(set(paths))  # no two supplementary files share a managed path
    for lib in libs:
        assert lib.sync.pull().waiting == 0
        keys = [p.citation_key.casefold() for p in lib.papers()]
        assert len(keys) == len(set(keys))


def test_library_moves_to_the_folder_of_its_linked_pdfs_without_copying_them(
    tmp_path: Path, devices: Devices, make_pdf: Callable[..., Path]
) -> None:
    """The user's Mendeley folder (already synchronised) becomes the library folder: no PDF is copied or moved,
    every link becomes relative, and a second device sharing that folder opens the PDFs."""
    old_root = tmp_path / "JBibManagerLibrary"
    papers_dir = tmp_path / "Dropbox" / "Mendeley"
    (papers_dir / "2020").mkdir(parents=True)
    linked = Path(shutil.copy2(make_pdf("m.pdf"), papers_dir / "2020" / "Boyce - 2020 - Title.pdf"))
    plain = AppConfig.load(CONFIG_PATH, library_root=old_root, import_mode="copy", local_data_dir=tmp_path / "data-desk")
    lib = Library(plain, resolver=MetadataResolver.from_config(plain, client=devices.client))
    folder = lib.create_collection("Thesis")
    ref = lib.add_reference(meta("Boyce", year=2020), folder.id).paper
    lib.attach_pdf(ref.id, lib.prepare_attachment(linked), mode="link")
    managed = lib.ingest_pdf(make_pdf("n.pdf"), folder.id, metadata=meta("Arruda", year=1993)).paper
    assert lib.linked_folders_outside() == [(papers_dir.joinpath("2020").as_posix(), 1)]
    before = sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*.pdf") if "downloads" not in p.parts)

    with pytest.raises(Exception, match="already contains"):
        (tmp_path / "busy" / "pdfs").mkdir(parents=True)
        (tmp_path / "busy" / "pdfs" / "x.txt").write_text("x")
        lib.relocate(tmp_path / "busy")
    config = lib.relocate(papers_dir)
    assert config.library_root == papers_dir and not (old_root / "library.db").exists()

    desk = devices.library("desk", papers_dir)  # reopened there, now shared
    assert desk.get_paper(ref.id).relative_pdf_path == "2020/Boyce - 2020 - Title.pdf"
    assert desk.pdf_path(desk.get_paper(ref.id)) == linked and linked.is_file()
    assert desk.pdf_path(desk.get_paper(managed.id)) == papers_dir / "pdfs" / "1993" / "Arruda_1993.pdf"
    assert desk.linked_folders_outside() == []
    after = sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*.pdf") if "downloads" not in p.parts)
    assert len(after) == len(before) == 2 and "Dropbox/Mendeley/2020/Boyce - 2020 - Title.pdf" in after
    assert (papers_dir / "bibtex" / "thesis.bib").is_file()
    desk.sync_now()

    phone = devices.library("phone", papers_dir, **phone_config(devices, papers_dir))
    phone.sync_now()
    assert all(phone.pdf_available(p) for p in phone.papers()) and len(phone.papers()) == 2
