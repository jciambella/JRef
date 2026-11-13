"""Milestone 1 test-suite for ``bibmanager.core`` (headless and offline).

Sections
  1. Deterministic naming and collision suffixes
  2. Physical storage: one PDF on disk, many logical project folders
  3. BibTeX: one ``.bib`` per project, LaTeX title protection
  4. Metadata pipeline (mocked Crossref / arXiv / OpenAlex) and FTS5 search

API payloads below are synthetic: DOIs use Crossref's reserved test prefix 10.5555 and
author names are fictitious.
"""

from __future__ import annotations

import itertools
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import bibtexparser
import httpx
import pytest

from bibmanager.core import (
    AppConfig,
    Author,
    ForeignBibFileError,
    IngestStatus,
    Library,
    MetadataResolver,
    Paper,
    PaperMetadata,
)
from bibmanager.core.bibtex_manager import escape_latex, is_generated_bib, protect_title, render_entry
from bibmanager.core.extractor import clean_markup, find_arxiv_ids, find_dois, read_pdf
from bibmanager.core.storage import base_citation_key, collision_suffixes, normalize_key_component

from .conftest import CONFIG_PATH


# --------------------------------------------------------------------------- helpers
def authors(*surnames: str) -> list[Author]:
    return [Author(family=s, given="J.") for s in surnames]


def meta(*surnames: str, year: int | None, title: str = "On the mechanics of soft solids", **kw: Any) -> PaperMetadata:
    return PaperMetadata(title=title, authors=authors(*surnames), year=year, **kw)


def pdfs_on_disk(root: Path) -> list[str]:
    """Every PDF anywhere under the library root (relative POSIX paths)."""
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*.pdf"))


def bib_entries(path: Path) -> dict[str, dict[str, str]]:
    parsed = bibtexparser.parse_string(path.read_text(encoding="utf-8"))
    assert not parsed.failed_blocks, f"{path.name} is not valid BibTeX"
    return {e.key: {name: f.value for name, f in e.fields_dict.items()} for e in parsed.entries}


# =========================================================================== 1. naming
@pytest.mark.parametrize(
    ("surnames", "year", "expected"),
    [
        (("Anand",), 2023, "Anand_2023"),
        (("Ciambella", "Lancioni"), 2025, "Ciambella_Lancioni_2025"),
        (("Ciambella", "Lancioni", "Nardinocchi"), 2025, "Ciambella_et_al_2025"),
        (("Ciambella", "Lancioni", "Rossi", "Bianchi", "Verdi"), 2025, "Ciambella_et_al_2025"),
    ],
)
def test_base_key_follows_author_count_rules(surnames: tuple[str, ...], year: int, expected: str) -> None:
    assert base_citation_key(authors(*surnames), year) == expected


@pytest.mark.parametrize(
    ("surname", "expected"),
    [
        ("García-Márquez", "garcia_marquez"),
        ("van der Giessen", "van_der_giessen"),
        ("D'Annunzio", "dannunzio"),
        ("Müller", "muller"),
        ("Ørsted", "orsted"),
        ("  de  la Cruz ", "de_la_cruz"),
    ],
)
def test_surname_normalisation(surname: str, expected: str) -> None:
    assert normalize_key_component(surname) == expected


def test_compound_surnames_in_keys() -> None:
    key = base_citation_key([Author("van der Giessen", "Erik"), Author("García-Márquez", "Ana")], 1999)
    assert key == "Van_Der_Giessen_Garcia_Marquez_1999"


def test_no_deterministic_key_without_authors_or_year() -> None:
    assert base_citation_key([], 2023) is None
    assert base_citation_key(authors("Anand"), None) is None


def test_collision_suffix_sequence() -> None:
    sequence = list(itertools.islice(collision_suffixes(), 28))
    assert sequence[:4] == ["", "a", "b", "c"]
    assert sequence[26:28] == ["z", "aa"]


def test_collisions_get_a_b_suffixes_and_filenames_equal_keys(library: Library, make_pdf: Callable[..., Path]) -> None:
    keys = []
    for i in range(3):
        result = library.ingest_pdf(
            make_pdf(f"download_{i}.pdf"),
            metadata=meta("Ciambella", "Lancioni", "Nardinocchi", year=2025, title=f"Different paper {i}"),
        )
        assert result.status is IngestStatus.IMPORTED
        assert Path(result.paper.relative_pdf_path).stem == result.paper.citation_key
        keys.append(result.paper.citation_key)

    assert keys == ["Ciambella_et_al_2025", "Ciambella_et_al_2025a", "Ciambella_et_al_2025b"]
    assert pdfs_on_disk(library.root) == [
        "pdfs/2025/Ciambella_et_al_2025.pdf",
        "pdfs/2025/Ciambella_et_al_2025a.pdf",
        "pdfs/2025/Ciambella_et_al_2025b.pdf",
    ]


def test_collision_with_untracked_file_already_on_disk(library: Library, make_pdf: Callable[..., Path]) -> None:
    stray = library.root / "pdfs" / "2023" / "Anand_2023.pdf"
    stray.parent.mkdir(parents=True)
    stray.write_bytes(b"%PDF-1.4 stray file not in the database")

    result = library.ingest_pdf(make_pdf(), metadata=meta("Anand", year=2023))

    assert result.paper.citation_key == "Anand_2023a"
    assert stray.read_bytes() == b"%PDF-1.4 stray file not in the database"


def test_reimporting_identical_pdf_is_deduplicated_not_suffixed(
    library: Library, make_pdf: Callable[..., Path]
) -> None:
    pdf = make_pdf("original.pdf")
    copy = shutil.copy2(pdf, pdf.with_name("same_bytes_other_name.pdf"))

    first = library.ingest_pdf(pdf, metadata=meta("Anand", year=2023))
    second = library.ingest_pdf(copy, metadata=meta("Anand", year=2023))

    assert second.status is IngestStatus.DUPLICATE_HASH
    assert second.paper.id == first.paper.id
    assert library.db.count_papers() == 1
    assert pdfs_on_disk(library.root) == ["pdfs/2023/Anand_2023.pdf"]
    assert Path(copy).exists()  # a duplicate drop never moves or deletes the user's file


# =========================================================================== 2. physical storage
def test_single_physical_copy_when_assigned_to_two_projects(library: Library, make_pdf: Callable[..., Path]) -> None:
    fracture = library.create_collection("Viscoelastic fracture")
    pinn = library.create_collection("PINN constitutive")
    pdf = make_pdf("S0022509623001234.pdf")
    dropped_again = shutil.copy2(pdf, pdf.with_name("same paper dropped again.pdf"))

    result = library.ingest_pdf(pdf, fracture.id, metadata=meta("Anand", year=2023))
    assert result.status is IngestStatus.IMPORTED
    assert not pdf.exists(), "import_mode='move' must move the PDF out of the download folder"
    filed = library.root / "pdfs" / "2023" / "Anand_2023.pdf"
    assert library.pdf_path(result.paper) == filed and filed.is_file()

    # Second project, twice: internal assignment (table -> folder) and a second external drop.
    assert library.add_to_collection(result.paper.id, pinn.id) is True
    assert library.add_to_collection(result.paper.id, pinn.id) is False  # idempotent
    again = library.ingest_pdf(dropped_again, pinn.id)  # hash dedup: no metadata lookup needed
    assert again.status is IngestStatus.DUPLICATE_HASH and again.paper.id == result.paper.id

    assert pdfs_on_disk(library.root) == ["pdfs/2023/Anand_2023.pdf"]
    assert sorted(p.name for p in library.root.iterdir()) == ["bibtex", "library.db", "pdfs"]
    assert {c.slug for c in library.collections_of(result.paper.id)} == {"viscoelastic_fracture", "pinn_constitutive"}
    for collection in (fracture, pinn):
        assert [p.citation_key for p in library.papers(collection.id)] == ["Anand_2023"]
        assert "Anand_2023" in bib_entries(library.bib.collection_path(collection.slug))


def test_remove_from_project_keeps_pdf_and_rewrites_only_that_bib(
    library: Library, make_pdf: Callable[..., Path]
) -> None:
    a = library.create_collection("Project A")
    b = library.create_collection("Project B")
    paper = library.ingest_pdf(make_pdf(), a.id, metadata=meta("Anand", year=2023)).paper
    library.add_to_collection(paper.id, b.id)

    assert library.remove_from_collection(paper.id, a.id) is True

    assert library.pdf_path(paper).is_file()
    assert bib_entries(library.bib.collection_path(a.slug)) == {}
    assert "Anand_2023" in bib_entries(library.bib.collection_path(b.slug))
    assert "Anand_2023" in bib_entries(library.bib.master_path)
    assert [p.id for p in library.unassigned_papers()] == []
    library.remove_from_collection(paper.id, b.id)
    assert [p.id for p in library.unassigned_papers()] == [paper.id]


def test_delete_from_library_removes_pdf_row_and_bib_entries(library: Library, make_pdf: Callable[..., Path]) -> None:
    a = library.create_collection("Project A")
    b = library.create_collection("Project B")
    paper = library.ingest_pdf(make_pdf(), a.id, metadata=meta("Anand", year=2023)).paper
    library.add_to_collection(paper.id, b.id)

    library.delete_paper(paper.id)

    assert pdfs_on_disk(library.root) == []
    assert library.db.count_papers() == 0
    for path in (library.bib.collection_path(a.slug), library.bib.collection_path(b.slug), library.bib.master_path):
        assert bib_entries(path) == {}


def test_year_and_author_edits_rename_and_move_the_pdf(library: Library, make_pdf: Callable[..., Path]) -> None:
    project = library.create_collection("Viscoelastic fracture")
    paper = library.ingest_pdf(make_pdf(), project.id, metadata=meta("Anand", year=2023)).paper

    moved = library.update_metadata(paper.id, year=2024)
    assert moved.citation_key == "Anand_2024"
    assert moved.relative_pdf_path == "pdfs/2024/Anand_2024.pdf"
    assert pdfs_on_disk(library.root) == ["pdfs/2024/Anand_2024.pdf"]
    assert set(bib_entries(library.bib.collection_path(project.slug))) == {"Anand_2024"}

    renamed = library.update_metadata(paper.id, authors="Anand, Lallit; Rossi, Mario")
    assert renamed.citation_key == "Anand_Rossi_2024"
    assert pdfs_on_disk(library.root) == ["pdfs/2024/Anand_Rossi_2024.pdf"]
    assert set(bib_entries(library.bib.collection_path(project.slug))) == {"Anand_Rossi_2024"}
    assert set(bib_entries(library.bib.master_path)) == {"Anand_Rossi_2024"}


def test_non_naming_edits_keep_suffixed_keys_stable(library: Library, make_pdf: Callable[..., Path]) -> None:
    library.ingest_pdf(make_pdf(), metadata=meta("Anand", year=2023, title="First"))
    second = library.ingest_pdf(make_pdf(), metadata=meta("Anand", year=2023, title="Second")).paper
    assert second.citation_key == "Anand_2023a"

    edited = library.update_metadata(second.id, title="Second, corrected", authors=[Author("Anand", "Lallit")])

    assert edited.citation_key == "Anand_2023a"
    assert edited.relative_pdf_path == "pdfs/2023/Anand_2023a.pdf"


def test_unresolved_metadata_goes_to_unknown_then_is_refiled(library: Library, make_pdf: Callable[..., Path]) -> None:
    result = library.ingest_pdf(make_pdf("Scan 12 (final).pdf"), metadata=PaperMetadata())

    paper = result.paper
    assert paper.needs_review and paper.meta.year is None
    assert paper.relative_pdf_path == "pdfs/Unknown/scan_12_final.pdf"
    assert paper.citation_key == "scan_12_final"
    assert [p.id for p in library.papers_needing_review()] == [paper.id]

    fixed = library.update_metadata(paper.id, authors=[Author("Lancioni", "Giovanni")], year="2025")

    assert fixed.citation_key == "Lancioni_2025" and not fixed.needs_review
    assert pdfs_on_disk(library.root) == ["pdfs/2025/Lancioni_2025.pdf"]
    assert library.papers_needing_review() == []


def test_same_doi_from_a_different_file_links_the_existing_paper(
    library: Library, make_pdf: Callable[..., Path]
) -> None:
    first = library.ingest_pdf(make_pdf("v1.pdf"), metadata=meta("Anand", year=2023, doi="10.5555/ABC.2023.001"))
    project = library.create_collection("Draft")

    second = library.ingest_pdf(
        make_pdf("v2.pdf"), project.id, metadata=meta("Anand", year=2023, doi="https://doi.org/10.5555/abc.2023.001")
    )

    assert second.status is IngestStatus.DUPLICATE_DOI and second.paper.id == first.paper.id
    assert pdfs_on_disk(library.root) == ["pdfs/2023/Anand_2023.pdf"]
    assert [p.id for p in library.papers(project.id)] == [first.paper.id]


# =========================================================================== 3. BibTeX
@pytest.mark.parametrize(
    ("title", "expected"),
    [
        (
            "Physics-informed neural networks (PINNs) for 3D hyperelasticity",
            "Physics-informed neural networks ({PINNs}) for {3D} hyperelasticity",
        ),
        (
            "On the Cauchy stress and the first Piola-Kirchhoff tensor",
            "On the {Cauchy} stress and the first {Piola}-{Kirchhoff} tensor",
        ),
        ("Navier-Stokes flow past Cosserat rods", "{Navier}-{Stokes} flow past {Cosserat} rods"),
        (
            "Mooney-Rivlin and Ogden models in Abaqus and FEniCSx",
            "{Mooney}-{Rivlin} and {Ogden} models in {Abaqus} and {FEniCSx}",
        ),
        ("Cauchy's theorem revisited", "{Cauchy}'s theorem revisited"),
        ("Mode II fracture: Part I", "Mode {II} fracture: Part {I}"),
        ("SHM of bridges via RVE homogenisation", "{SHM} of bridges via {RVE} homogenisation"),
        ("Already {Protected} title with $\\sigma_{ij}$ math", "Already {Protected} title with $\\sigma_{ij}$ math"),
        ("Theory & practice of 100% green concrete", "Theory \\& practice of 100\\% green concrete"),
        ("a plain lowercase title", "a plain lowercase title"),
    ],
)
def test_title_protection(title: str, expected: str, protected_words: tuple[str, ...]) -> None:
    protected = protect_title(escape_latex(title), protected_words)
    assert protected == expected
    assert protect_title(protected, protected_words) == protected  # idempotent


def test_escape_latex_leaves_math_untouched() -> None:
    assert escape_latex("A & B: 5% of $a_1 \\& b$ #1") == "A \\& B: 5\\% of $a_1 \\& b$ \\#1"
    assert clean_markup("TiO<sub>2</sub> films &amp; <i>in situ</i> tests") == "TiO$_{2}$ films & in situ tests"


def test_entry_contains_clean_fields_and_omits_abstract(protected_words: tuple[str, ...]) -> None:
    paper = Paper(
        id=1,
        sha256="0" * 64,
        citation_key="Van_Der_Giessen_Rossi_1999",
        relative_pdf_path="pdfs/1999/Van_Der_Giessen_Rossi_1999.pdf",
        meta=PaperMetadata(
            title="Cohesive zones in 3D",
            authors=[Author("van der Giessen", "Erik"), Author("Rossi", "Mario")],
            year=1999,
            journal="J. Mech. Phys. Solids",
            volume="47",
            issue="3",
            pages="123-145",
            doi="10.5555/jmps.1999.47",
            abstract="This must not be exported.",
        ),
    )
    entries = bib_entries_from_text(render_entry(paper, protected_words))
    assert entries == {
        "Van_Der_Giessen_Rossi_1999": {
            "author": "van der Giessen, Erik and Rossi, Mario",
            "title": "Cohesive zones in {3D}",
            "journal": "J. Mech. Phys. Solids",
            "year": "1999",
            "volume": "47",
            "number": "3",
            "pages": "123--145",
            "doi": "10.5555/jmps.1999.47",
        }
    }


def bib_entries_from_text(text: str) -> dict[str, dict[str, str]]:
    parsed = bibtexparser.parse_string(text)
    assert not parsed.failed_blocks
    return {e.key: {name: f.value for name, f in e.fields_dict.items()} for e in parsed.entries}


def test_two_projects_generate_two_separate_protected_bib_files(
    library: Library, make_pdf: Callable[..., Path], tmp_path: Path
) -> None:
    manuscript = tmp_path / "manuscript_repo"
    manuscript.mkdir()
    fracture = library.create_collection("Viscoelastic fracture")
    pinn = library.create_collection("PINN constitutive", bib_export_path=manuscript / "references.bib")

    library.ingest_pdf(
        make_pdf(),
        fracture.id,
        metadata=meta(
            "Anand",
            year=2023,
            title="Cauchy stress in Mooney-Rivlin solids: Mode II fracture",
            journal="Journal of the Mechanics and Physics of Solids",
        ),
    )
    library.ingest_pdf(
        make_pdf(),
        pinn.id,
        metadata=meta(
            "Ciambella",
            "Lancioni",
            "Nardinocchi",
            year=2025,
            title="Physics-informed neural networks (PINNs) for 3D finite elasticity in FEniCSx",
        ),
    )
    shared = library.ingest_pdf(
        make_pdf(),
        fracture.id,
        metadata=meta("Lancioni", "Ciambella", year=2025, title="A Cosserat model for Piola-Kirchhoff stresses"),
    ).paper
    library.add_to_collection(shared.id, pinn.id)

    bib_dir = library.root / "bibtex"
    assert sorted(p.name for p in bib_dir.glob("*.bib")) == [
        "master.bib",
        "pinn_constitutive.bib",
        "viscoelastic_fracture.bib",
    ]
    fracture_bib = bib_entries(bib_dir / "viscoelastic_fracture.bib")
    pinn_bib = bib_entries(bib_dir / "pinn_constitutive.bib")
    master_bib = bib_entries(bib_dir / "master.bib")

    assert set(fracture_bib) == {"Anand_2023", "Lancioni_Ciambella_2025"}
    assert set(pinn_bib) == {"Ciambella_et_al_2025", "Lancioni_Ciambella_2025"}
    assert set(master_bib) == {"Anand_2023", "Ciambella_et_al_2025", "Lancioni_Ciambella_2025"}

    assert fracture_bib["Anand_2023"]["title"] == "{Cauchy} stress in {Mooney}-{Rivlin} solids: Mode {II} fracture"
    assert fracture_bib["Anand_2023"]["journal"] == "Journal of the Mechanics and Physics of Solids"
    assert pinn_bib["Ciambella_et_al_2025"]["title"] == (
        "Physics-informed neural networks ({PINNs}) for {3D} finite elasticity in {FEniCSx}"
    )
    assert pinn_bib["Ciambella_et_al_2025"]["author"] == "Ciambella, J. and Lancioni, J. and Nardinocchi, J."
    assert pinn_bib["Lancioni_Ciambella_2025"]["title"] == "A {Cosserat} model for {Piola}-{Kirchhoff} stresses"

    # Citation key == PDF filename stem for every exported entry.
    for key in master_bib:
        paper = library.db.get_paper_by_citation_key(key)
        assert paper is not None and Path(paper.relative_pdf_path).stem == key

    # External target inside the manuscript repository mirrors the project file byte for byte.
    assert (manuscript / "references.bib").read_text(encoding="utf-8") == (bib_dir / "pinn_constitutive.bib").read_text(
        encoding="utf-8"
    )

    # Membership change -> immediate rewrite of the affected project file only.
    library.remove_from_collection(shared.id, pinn.id)
    assert set(bib_entries(bib_dir / "pinn_constitutive.bib")) == {"Ciambella_et_al_2025"}
    assert set(bib_entries(manuscript / "references.bib")) == {"Ciambella_et_al_2025"}
    assert set(bib_entries(bib_dir / "viscoelastic_fracture.bib")) == {"Anand_2023", "Lancioni_Ciambella_2025"}


def test_bib_files_are_rewritten_only_when_content_changes(library: Library, make_pdf: Callable[..., Path]) -> None:
    project = library.create_collection("Draft")
    library.ingest_pdf(make_pdf(), project.id, metadata=meta("Anand", year=2023))
    path = library.bib.collection_path(project.slug)
    before = path.stat().st_mtime_ns

    library.sync_all_bibs()

    assert path.stat().st_mtime_ns == before
    assert is_generated_bib(path)


def test_external_target_never_clobbers_a_hand_written_bib(library: Library, tmp_path: Path) -> None:
    foreign = tmp_path / "refs.bib"
    foreign.write_text("@book{knuth1984, title = {The TeXbook}}\n", encoding="utf-8")
    project = library.create_collection("Paper draft")

    with pytest.raises(ForeignBibFileError):
        library.set_collection_export_path(project.id, foreign)
    assert foreign.read_text(encoding="utf-8").startswith("@book{knuth1984")
    with pytest.raises(ValueError):
        library.set_collection_export_path(project.id, tmp_path / "main.tex")

    library.set_collection_export_path(project.id, foreign, overwrite_foreign=True)
    assert is_generated_bib(foreign)


def test_rename_and_delete_project_manage_bib_files(library: Library, make_pdf: Callable[..., Path]) -> None:
    project = library.create_collection("Draft")
    paper = library.ingest_pdf(make_pdf(), project.id, metadata=meta("Anand", year=2023)).paper
    reserved = library.create_collection("Master")
    assert reserved.slug == "master_2"  # never clobbers master.bib

    renamed = library.rename_collection(project.id, "SHM bridges")
    assert renamed.slug == "shm_bridges"
    assert not (library.root / "bibtex" / "draft.bib").exists()
    assert set(bib_entries(library.root / "bibtex" / "shm_bridges.bib")) == {"Anand_2023"}

    library.delete_collection(project.id)
    assert not (library.root / "bibtex" / "shm_bridges.bib").exists()
    assert library.pdf_path(paper).is_file()
    assert set(bib_entries(library.bib.master_path)) == {"Anand_2023"}


def test_config_ships_solid_mechanics_protected_words(protected_words: tuple[str, ...]) -> None:
    for word in (
        "Cauchy",
        "Piola",
        "Kirchhoff",
        "Cosserat",
        "FEniCSx",
        "Abaqus",
        "Navier",
        "Stokes",
        "Rivlin",
        "Mooney",
        "Ogden",
    ):
        assert word in protected_words
    assert AppConfig.load(CONFIG_PATH).user_agent.startswith("JRef/1.0 (mailto:")


# =========================================================================== 4. extraction & FTS
TEST_DOI = "10.5555/test.2023.001"
CROSSREF_MESSAGE = {
    "DOI": TEST_DOI.upper(),
    "type": "journal-article",
    "title": ["Phase-field fracture of hydrogels under Cauchy traction"],
    "author": [{"given": "Mario", "family": "Rossi"}, {"given": "Giulia", "family": "Bianchi"}],
    "container-title": ["Journal of Synthetic Test Mechanics"],
    "volume": "178",
    "issue": "4",
    "page": "105-121",
    "published-online": {"date-parts": [[2022, 12, 20]]},
    "published-print": {"date-parts": [[2023, 2]]},
    "publisher": "Test Publisher",
    "abstract": "<jats:title>Abstract</jats:title><jats:p>We model H<sub>2</sub>O transport.</jats:p>",
}


def _feed(entry: str = "") -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom">'
        f"{entry}</feed>"
    )


def api_resolver(
    config: AppConfig,
    seen: list[httpx.Request],
    *,
    crossref: dict[str, dict[str, Any]] | None = None,
    arxiv: dict[str, str] | None = None,
    openalex: list[dict[str, Any]] | None = None,
) -> MetadataResolver:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.host == "api.crossref.org":
            doi = unquote(request.url.path.removeprefix("/works/")).lower()
            if crossref and doi in crossref:
                return httpx.Response(200, json={"status": "ok", "message": crossref[doi]})
            return httpx.Response(404, text="Resource not found.")
        if request.url.host == "export.arxiv.org":
            return httpx.Response(200, text=(arxiv or {}).get(request.url.params["id_list"], _feed()))
        if request.url.host == "api.openalex.org":
            return httpx.Response(200, json={"results": openalex or []})
        raise AssertionError(f"unexpected host {request.url}")

    return MetadataResolver.from_config(config, client=httpx.Client(transport=httpx.MockTransport(handler)))


def test_identifier_regexes() -> None:
    text = "Available online. https://doi.org/10.5555/abc.2023.105345. See also (doi:10.5555/PhysRevE.99.012345)."
    assert find_dois(text) == ["10.5555/abc.2023.105345", "10.5555/physreve.99.012345"]
    stamp = "arXiv:2301.12345v2 [cs.LG] 12 Jan 2023; old style https://arxiv.org/abs/cond-mat/0601001v1"
    assert find_arxiv_ids(stamp) == ["2301.12345", "cond-mat/0601001"]
    assert find_arxiv_ids("plain number 2301.12345 without the arXiv cue") == []


def test_read_pdf_extracts_identifiers_and_largest_font_title(make_pdf: Callable[..., Path]) -> None:
    pdf = make_pdf(
        title="Phase-field fracture of soft solids",
        lines=["Mario Rossi, Giulia Bianchi", f"https://doi.org/{TEST_DOI}"],
    )
    content = read_pdf(pdf)
    assert find_dois(content.page(0)) == [TEST_DOI]
    assert content.title_candidates == ["Phase-field fracture of soft solids"]
    assert content.author_hint == "Mario Rossi, Giulia Bianchi"


def test_crossref_pipeline_names_files_and_sends_polite_user_agent(
    config: AppConfig, make_pdf: Callable[..., Path]
) -> None:
    seen: list[httpx.Request] = []
    resolver = api_resolver(config, seen, crossref={TEST_DOI: CROSSREF_MESSAGE})
    pdf = make_pdf(
        "1-s2.0-S0000000000000000-main.pdf",
        title=CROSSREF_MESSAGE["title"][0],
        lines=["M. Rossi, G. Bianchi", f"https://doi.org/{TEST_DOI}", "Hydrogels crack under Cauchy traction."],
    )
    with Library(config, resolver=resolver) as lib:
        project = lib.create_collection("Hydrogel fracture")
        result = lib.ingest_pdf(pdf, project.id)
        paper = result.paper

        assert result.status is IngestStatus.IMPORTED
        assert paper.citation_key == "Rossi_Bianchi_2023"  # print year preferred over online-first year
        assert paper.relative_pdf_path == "pdfs/2023/Rossi_Bianchi_2023.pdf"
        assert not paper.needs_review
        assert paper.meta.doi == TEST_DOI
        assert paper.meta.abstract == "We model H$_{2}$O transport."
        entry = bib_entries(lib.bib.collection_path(project.slug))["Rossi_Bianchi_2023"]
        assert entry["title"] == "Phase-field fracture of hydrogels under {Cauchy} traction"
        assert entry["pages"] == "105--121" and entry["number"] == "4"
        assert [h.paper_id for h in lib.search_fulltext("hydrogels traction")] == [paper.id]

    assert seen, "Crossref was never queried"
    assert all(r.headers["User-Agent"] == config.user_agent for r in seen)


def test_arxiv_pipeline_follows_linked_doi_to_crossref(config: AppConfig, make_pdf: Callable[..., Path]) -> None:
    feed = _feed(
        "<entry><id>http://arxiv.org/abs/2301.00001v2</id>"
        "<published>2023-01-03T00:00:00Z</published>"
        "<title>Phase-field fracture of hydrogels\n under Cauchy traction</title>"
        "<summary>Preprint abstract.</summary>"
        "<author><name>Mario Rossi</name></author><author><name>Giulia Bianchi</name></author>"
        f"<arxiv:doi>{TEST_DOI}</arxiv:doi></entry>"
    )
    seen: list[httpx.Request] = []
    resolver = api_resolver(config, seen, crossref={TEST_DOI: CROSSREF_MESSAGE}, arxiv={"2301.00001": feed})
    pdf = make_pdf(title="Phase-field fracture of hydrogels", lines=["arXiv:2301.00001v2 [cond-mat.soft] 3 Jan 2023"])
    with Library(config, resolver=resolver) as lib:
        result = lib.ingest_pdf(pdf)

    assert result.paper.citation_key == "Rossi_Bianchi_2023"
    assert result.paper.meta.arxiv_id == "2301.00001"
    assert result.paper.meta.journal == "Journal of Synthetic Test Mechanics"
    assert any("arxiv+crossref" in line for line in result.log)


def test_openalex_title_fallback_when_no_identifier(config: AppConfig, make_pdf: Callable[..., Path]) -> None:
    title = "Viscoelastic dissipation in bituminous binders under cyclic loading"
    works = [
        {"title": "A completely unrelated topic in hydrology", "publication_year": 2020},
        {
            "title": title,
            "type": "article",
            "publication_year": 2022,
            "authorships": [
                {"author": {"display_name": "Maria Teresa De Luca"}},
                {"author": {"display_name": "Paolo Verdi"}},
            ],
            "primary_location": {"source": {"display_name": "Fictional Journal of Rheology"}},
            "biblio": {"volume": "12", "issue": "3", "first_page": "101", "last_page": "118"},
            "doi": None,
        },
    ]
    seen: list[httpx.Request] = []
    resolver = api_resolver(config, seen, openalex=works)
    with Library(config, resolver=resolver) as lib:
        result = lib.ingest_pdf(make_pdf("scan.pdf", title=title, lines=["M. T. De Luca and P. Verdi"]))

    paper = result.paper
    assert paper.citation_key == "De_Luca_Verdi_2022"
    assert paper.meta.authors[0] == Author(family="De Luca", given="Maria Teresa")
    assert paper.meta.pages == "101--118" and paper.meta.journal == "Fictional Journal of Rheology"
    assert not paper.needs_review
    assert any(r.url.host == "api.openalex.org" for r in seen)


def test_reference_list_doi_on_page_two_is_not_trusted(config: AppConfig, make_pdf: Callable[..., Path]) -> None:
    cited = dict(CROSSREF_MESSAGE, title=["Some other cited paper"], DOI="10.5555/cited.001")
    seen: list[httpx.Request] = []
    resolver = api_resolver(config, seen, crossref={"10.5555/cited.001": cited})
    pdf = make_pdf(
        "unknown_scan.pdf",
        title="Notes on rubber elasticity",
        page2_lines=["References", "[1] M. Rossi, Some other cited paper, doi:10.5555/cited.001"],
    )
    with Library(config, resolver=resolver) as lib:
        paper = lib.ingest_pdf(pdf).paper
        master = lib.bib.master_path.read_text(encoding="utf-8")

    assert paper.needs_review
    assert paper.meta.title == "Notes on rubber elasticity"  # on-page title kept as a review hint
    assert paper.meta.doi == ""  # the cited DOI was rejected
    assert paper.citation_key == "unknown_scan"
    assert paper.relative_pdf_path == "pdfs/Unknown/unknown_scan.pdf"
    assert "needs review" in master and "@article{unknown_scan," in master


def test_fulltext_and_metadata_search(library: Library, make_pdf: Callable[..., Path]) -> None:
    rubber = library.ingest_pdf(
        make_pdf(lines=["The Mullins effect in filled elastomers is examined.", "Hysteresis loops were measured."]),
        metadata=meta("Rossi", year=2021, title="Stress softening in rubber"),
    ).paper
    bridge = library.ingest_pdf(
        make_pdf(lines=["Operational modal analysis of a concrete bridge deck."]),
        metadata=meta("Bianchi", year=2020, title="Bridge monitoring"),
    ).paper

    hits = library.search_fulltext("mullins elastomers")
    assert [h.paper_id for h in hits] == [rubber.id]
    assert "[" in hits[0].snippet and "]" in hits[0].snippet
    assert [h.paper_id for h in library.search_fulltext("modal*")] == [bridge.id]
    assert library.search_fulltext('"unbalanced quote AND (') == []  # user input never breaks FTS5 syntax
    assert [p.id for p in library.search_metadata("rossi")] == [rubber.id]

    library.delete_paper(rubber.id)
    assert library.search_fulltext("mullins") == []


# =========================================================================== 5. schema v2 and GUI support API
def test_schema_v1_library_is_migrated_in_place(tmp_path: Path) -> None:
    import sqlite3

    from bibmanager.core.database import SCHEMA_VERSION, Database

    path = tmp_path / "library.db"
    with sqlite3.connect(path) as conn:  # the Milestone 1 (v1) tables, verbatim column sets
        conn.executescript(
            """
            CREATE TABLE papers (
                id INTEGER PRIMARY KEY AUTOINCREMENT, sha256 TEXT NOT NULL UNIQUE, doi TEXT, arxiv_id TEXT,
                citation_key TEXT NOT NULL UNIQUE, entry_type TEXT NOT NULL DEFAULT 'article',
                title TEXT NOT NULL DEFAULT '', authors_json TEXT NOT NULL DEFAULT '[]', year INTEGER,
                journal TEXT, volume TEXT, issue TEXT, pages TEXT, publisher TEXT, abstract TEXT,
                relative_pdf_path TEXT NOT NULL UNIQUE, needs_review INTEGER NOT NULL DEFAULT 0, notes TEXT,
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')));
            CREATE TABLE collections (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
                slug TEXT NOT NULL UNIQUE, parent_id INTEGER REFERENCES collections(id) ON DELETE CASCADE,
                bib_export_path TEXT, created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')));
            CREATE TABLE paper_collections (
                paper_id INTEGER NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
                collection_id INTEGER NOT NULL REFERENCES collections(id) ON DELETE CASCADE,
                added_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
                PRIMARY KEY (paper_id, collection_id)) WITHOUT ROWID;
            INSERT INTO papers (id, sha256, citation_key, title, year, relative_pdf_path)
                VALUES (7, 'x', 'anand_2023', 'Old row', 2023, 'pdfs/2023/anand_2023.pdf');
            INSERT INTO collections (id, name, slug) VALUES (1, 'Thesis', 'thesis');
            INSERT INTO paper_collections (paper_id, collection_id) VALUES (7, 1);
            PRAGMA user_version = 1;
            """
        )
    with Database(path) as db:
        paper = db.get_paper_by_citation_key("Anand_2023")  # keys compare case-insensitively from v3 on
        assert paper is not None and paper.id == 7 and paper.meta.title == "Old row"
        assert paper.citation_key == "anand_2023"  # existing keys are never rewritten by an upgrade
        assert (paper.favorite, paper.is_read, paper.tags, paper.meta.keywords) == (False, False, "", "")
        assert (paper.pdf_linked, paper.pdf_managed, paper.meta.extra_fields) == (False, True, {})
        assert [c.name for c in db.collections_of_paper(7)] == ["Thesis"]  # folder links survive the rebuild
        assert db.conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION == 5
        notnull = {r["name"]: r["notnull"] for r in db.conn.execute("PRAGMA table_info(papers)")}
        assert notnull["sha256"] == 0 and notnull["relative_pdf_path"] == 0
        assert db.conn.execute("PRAGMA foreign_key_check").fetchall() == []
        with pytest.raises(sqlite3.IntegrityError):  # case-insensitive uniqueness
            db.insert_paper(sha256="y", citation_key="ANAND_2023", relative_pdf_path=None, meta=PaperMetadata())
        db.delete_paper(7)  # FTS trigger was re-created with the new table
        assert db.conn.execute("SELECT COUNT(*) FROM paper_collections").fetchone()[0] == 0


def test_flags_views_and_thread_safe_duplicate_check(library: Library, make_pdf: Callable[..., Path]) -> None:
    import threading

    a = library.ingest_pdf(make_pdf(), metadata=meta("Anand", year=2023)).paper
    b = library.ingest_pdf(make_pdf(), metadata=meta("Rossi", year=2021)).paper
    library.update_flags(a.id, favorite=True, tags="rubber;  fracture ; rubber", my_publication=True)
    opened = library.mark_opened(b.id)

    assert library.get_paper(a.id).tags == "rubber; fracture"
    assert [p.id for p in library.favorite_papers()] == [a.id] == [p.id for p in library.my_publications()]
    assert opened.is_read and opened.last_opened_at
    assert [p.id for p in library.recently_read()] == [b.id]
    assert {p.id for p in library.recently_added()} == {a.id, b.id}
    assert set(bib_entries(library.bib.master_path)) == {"Anand_2023", "Rossi_2021"}  # flags never touch keys

    results: list[bool] = []
    worker = threading.Thread(
        target=lambda: results.extend([library.known_sha256(a.sha256), library.known_sha256("0")])
    )
    worker.start()
    worker.join()
    assert results == [True, False]


def test_fetched_metadata_is_merged_not_blanked(library: Library, make_pdf: Callable[..., Path]) -> None:
    paper = library.ingest_pdf(
        make_pdf(), metadata=meta("Anand", year=2023, abstract="Hand-written abstract", keywords="rubber")
    ).paper
    fetched = PaperMetadata(
        title="Fetched title", authors=authors("Anand", "Rossi"), year=2024, journal="J. Test", doi="10.5555/X1"
    )

    merged = library.apply_metadata(paper.id, fetched)

    assert merged.citation_key == "Anand_Rossi_2024"
    assert merged.meta.abstract == "Hand-written abstract" and merged.meta.keywords == "rubber"
    assert merged.meta.doi == "10.5555/x1" and merged.meta.journal == "J. Test"
    assert pdfs_on_disk(library.root) == ["pdfs/2024/Anand_Rossi_2024.pdf"]


def test_crossref_catalog_ids_are_parsed() -> None:
    from bibmanager.core.extractor import parse_crossref_message

    parsed = parse_crossref_message(
        dict(CROSSREF_MESSAGE, ISSN=["0022-5096", "1873-4782"], URL="https://doi.org/10.5555/test.2023.001")
    )
    assert parsed.issn == "0022-5096; 1873-4782"
    assert parsed.url == "https://doi.org/10.5555/test.2023.001"


def test_navigation_counts(library: Library, make_pdf: Callable[..., Path]) -> None:
    folder = library.create_collection("Draft")
    a = library.ingest_pdf(make_pdf(), folder.id, metadata=meta("Anand", year=2023)).paper
    b = library.ingest_pdf(make_pdf(), metadata=meta("Rossi", year=2021)).paper
    library.ingest_pdf(make_pdf("scan.pdf"), metadata=PaperMetadata())
    library.update_flags(b.id, favorite=True)
    assert library.counts() == {
        "all": 3, "favorites": 1, "review": 1, "mine": 0, "unsorted": 2, f"folder:{folder.id}": 1,
    }  # fmt: skip
    assert a.id != b.id


def test_user_settings_overlay_and_extra_protected_words(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    user_file = tmp_path / "user" / "config.json"
    base = AppConfig.load(CONFIG_PATH)
    edited = AppConfig(
        library_root=tmp_path / "MyLibrary",
        protected_words=base.protected_words,
        import_mode="copy",
        contact_email="someone@example.org",
        extra_protected_words=("Duffing", "Zener"),
    )
    edited.save_user_settings(user_file)

    monkeypatch.setenv("BIBMANAGER_CONFIG", str(user_file))
    loaded = AppConfig.load()
    assert loaded.library_root == tmp_path / "MyLibrary"
    assert loaded.import_mode == "copy" and loaded.contact_email == "someone@example.org"
    assert "Duffing" in loaded.protected_words and "Cauchy" in loaded.protected_words  # defaults still apply
    assert loaded.extra_protected_words == ("Duffing", "Zener")
    assert loaded.http_timeout_s == base.http_timeout_s


# =========================================================================== 6. Rebrands (JBibManager v0.5, JRef v0.6)
def test_bib_files_of_earlier_releases_are_still_recognised(tmp_path: Path) -> None:
    from bibmanager.core.bibtex_manager import GENERATED_MARKER

    old = tmp_path / "old.bib"
    old.write_text("% Generated by bibmanager -- master library (0 entries).\n", encoding="utf-8")
    older = tmp_path / "v05.bib"
    older.write_text('% Generated by JBibManager -- collection "Thesis" (0 entries).\n', encoding="utf-8")
    hand = tmp_path / "hand.bib"
    hand.write_text("@article{x, title = {y}}\n", encoding="utf-8")
    assert GENERATED_MARKER == "% Generated by JRef"
    assert is_generated_bib(old) and is_generated_bib(older) and not is_generated_bib(hand)


def test_settings_of_bibmanager_are_migrated_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import json
    import sys

    from bibmanager.core import config as cfg

    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv(cfg.CONFIG_ENV_VAR, raising=False)
    monkeypatch.chdir(tmp_path)
    legacy = tmp_path / "BibManager" / "config.json"
    legacy.parent.mkdir()
    legacy.write_text(json.dumps({"library_root": str(tmp_path / "lib"), "import_mode": "copy"}), encoding="utf-8")
    assert cfg.user_config_path() == tmp_path / "JRef" / "config.json"

    loaded = AppConfig.load()  # migrates, then reads the new per-user file
    assert (loaded.library_root, loaded.import_mode) == (tmp_path / "lib", "copy")
    assert cfg.user_config_path().is_file() and legacy.is_file()  # old file kept for older versions
    cfg.user_config_path().write_text('{"import_mode": "move"}', encoding="utf-8")
    assert cfg.migrate_legacy_user_config() is None  # never overwrites newer settings
    assert AppConfig.load().import_mode == "move"


def test_settings_of_jbibmanager_win_over_those_of_bibmanager(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import json
    import sys

    from bibmanager import APP_AUTHOR, APP_AUTHOR_EMAIL, APP_NAME, APP_SUBTITLE
    from bibmanager.core import config as cfg

    assert (APP_NAME, APP_SUBTITLE) == ("JRef", "Journal Reference Manager")
    assert (APP_AUTHOR, APP_AUTHOR_EMAIL) == ("JC", "j.c@outlook.com")
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv(cfg.CONFIG_ENV_VAR, raising=False)
    monkeypatch.chdir(tmp_path)
    for name, root in (("BibManager", "old"), ("JBibManager", "newer")):
        (tmp_path / name).mkdir()
        (tmp_path / name / "config.json").write_text(json.dumps({"library_root": str(tmp_path / root)}), encoding="utf-8")
    assert AppConfig.load().library_root == tmp_path / "newer"  # the most recent earlier name is taken over
    assert (tmp_path / "JRef" / "config.json").is_file() and (tmp_path / "JBibManager" / "config.json").is_file()


def test_schema_v3_library_gains_identities_and_keeps_its_content(tmp_path: Path) -> None:
    """A 0.5 database (schema v3: UNIQUE sha256/path/DOI, no uid) is upgraded in place, ids and full text kept."""
    import sqlite3

    from bibmanager.core.database import Database, collection_uid

    path = tmp_path / "library.db"
    with Database(path) as db:
        pid = db.insert_paper(sha256="a" * 64, citation_key="Anand_2023", relative_pdf_path="pdfs/2023/Anand_2023.pdf",
                              meta=meta("Anand", year=2023, doi="10.5555/v3"))  # fmt: skip
        db.index_fulltext(pid, db.get_paper(pid).meta, "viscoelastic body text")
        cid = db.create_collection("Thesis", "thesis").id
        db.add_paper_to_collection(pid, cid)
    conn = sqlite3.connect(path)  # make it look like v3: no uid columns, the unique DOI index of 0.5
    conn.executescript(
        "DROP INDEX ux_papers_uid; DROP INDEX ux_collections_uid; ALTER TABLE papers DROP COLUMN uid;"
        "ALTER TABLE collections DROP COLUMN uid; DROP INDEX ix_papers_doi;"
        "CREATE UNIQUE INDEX ux_papers_doi ON papers(doi) WHERE doi IS NOT NULL; PRAGMA user_version = 3;"
    )
    conn.close()

    with Database(path) as db:
        paper = db.get_paper(pid)
        assert paper is not None and paper.citation_key == "Anand_2023" and paper.meta.doi == "10.5555/v3"
        assert [h.paper_id for h in db.search_fulltext("viscoelastic")] == [pid]
        assert [p.id for p in db.papers_in_collection(cid)] == [pid]
        uids = [r[0] for r in db.conn.execute("SELECT uid FROM papers")]
        assert len(uids) == 1 and len(uids[0]) == 32
        assert db.conn.execute("SELECT uid FROM collections").fetchone()[0] == collection_uid("thesis")
        # the same DOI can now be stored twice (needed when two devices added the same paper before syncing)
        db.insert_paper(sha256=None, citation_key="Anand_2023a", relative_pdf_path=None, meta=meta("Anand", year=2023, doi="10.5555/v3"))
        db.delete_paper(pid)
        assert db.search_fulltext("viscoelastic") == []
