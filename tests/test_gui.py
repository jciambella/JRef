"""Headless GUI tests (Qt "offscreen" platform). Skipped automatically when PySide6 is absent.

They drive the real widgets: tree drops, inspector edits, background imports through the
QThreadPool, context actions and settings persistence.
"""

from __future__ import annotations

import json
import os
import shutil
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6.QtWidgets", exc_type=ImportError)  # also skips if Qt system libs are missing

import httpx  # noqa: E402
import pymupdf  # noqa: E402
from PySide6.QtCore import QMimeData, QSettings, Qt, QUrl  # noqa: E402
from PySide6.QtGui import QGuiApplication  # noqa: E402
from PySide6.QtWidgets import QApplication, QInputDialog, QMessageBox  # noqa: E402

from bibmanager.core import AppConfig, Author, Library, PaperMetadata  # noqa: E402
from bibmanager.gui import theme  # noqa: E402
from bibmanager.gui.main_window import SEARCH_FULLTEXT, MainWindow  # noqa: E402
from bibmanager.gui.paper_model import HEADERS, PAPER_ID_ROLE, Col  # noqa: E402
from bibmanager.gui.sidebar import NodeKind  # noqa: E402

from .test_core import CROSSREF_MESSAGE, TEST_DOI, api_resolver, bib_entries, meta, pdfs_on_disk  # noqa: E402


@pytest.fixture(scope="session")
def qapp() -> QApplication:
    app = QApplication.instance() or QApplication([])
    assert isinstance(app, QApplication)
    theme.apply(app, "light")
    return app


def _make_window(qapp: QApplication, library: Library, settings_path: Path, *, watch_inbox: bool = False) -> MainWindow:
    window = MainWindow(
        library,
        settings=QSettings(str(settings_path), QSettings.Format.IniFormat),
        user_config=settings_path.with_name("user-config.json"),
        watch_inbox=watch_inbox,
    )
    window.resize(1400, 900)
    window.show()
    qapp.processEvents()
    return window


@pytest.fixture
def launched(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Capture "open with the default application" requests instead of launching viewers."""
    calls: list[str] = []
    monkeypatch.setattr(
        MainWindow, "_launch", lambda self, url: calls.append(url.toLocalFile() or url.toString()) or True
    )
    return calls


@pytest.fixture
def window(qapp: QApplication, library: Library, tmp_path: Path, launched: list[str]) -> Iterator[MainWindow]:
    win = _make_window(qapp, library, tmp_path / "gui.ini")
    yield win
    win.runner.wait_idle()
    win.close()
    win.deleteLater()


def _ingest(library: Library, make_pdf: Callable[..., Path], *args: Any, **kwargs: Any) -> int:
    collection_id = kwargs.pop("collection_id", None)
    lines = kwargs.pop("lines", ())
    return library.ingest_pdf(make_pdf(lines=lines), collection_id, metadata=meta(*args, **kwargs)).paper.id


def _select(window: MainWindow, *paper_ids: int) -> None:
    window.select_ids(paper_ids)
    QApplication.processEvents()


def _proxy_index(window: MainWindow, paper_id: int, col: Col) -> Any:
    row = window.model.row_of(paper_id)
    assert row is not None
    return window.proxy.mapFromSource(window.model.index(row, int(col)))


# =========================================================================== layout
def test_layout_shelf_chips_rows_view_and_reference_sheet(window: MainWindow) -> None:
    root = window.tree.source.invisibleRootItem()
    names = [root.child(r).text() for r in range(root.rowCount())]
    assert names[:3] == ["Inbox", "Library", "Projects"]
    assert names[names.index("Saved searches") + 1 :] == ["No PDF yet", "Authored by me", "Recently read"]
    assert "New project" in names
    for mendeley_view in ("All documents", "Recently added", "Favorites", "Needs review", "My publications",
                          "Unsorted", "Folders"):  # fmt: skip
        assert mendeley_view not in names
    assert [a.text() for a in window.menuBar().actions()] == ["&File", "&Edit", "&View", "&Tools", "&Help"]
    header = window.table.horizontalHeader()
    assert window.view_mode == "rows" and header.isHidden()
    assert [c for c in Col if not header.isSectionHidden(int(c))] == [Col.REFERENCE]
    window.set_view_mode("table")
    visible = [Col(header.logicalIndex(v)) for v in range(header.count())]
    visible = [HEADERS[c] for c in visible if not header.isSectionHidden(int(c))]
    assert visible == ["Key", "Title", "Authors", "Year", "Venue", "Added", "", ""]
    assert [chip.text() for chip in window.filters.toggles.values()] == ["Starred", "Unread", "Has PDF", "In no project"]
    assert window.list_title.text() == "Library" and window.breadcrumb.text() == "<b>Library</b>"
    assert window.inspector.study_button.text() == "Study and ask AI"
    assert not window.table.empty_state.isHidden()  # empty library -> call to action
    assert window.import_button.defaultAction().text() == "Import PDFs"


def test_inbox_library_projects_saved_searches_and_chips(
    window: MainWindow, library: Library, make_pdf: Callable[..., Path]
) -> None:
    folder = library.create_collection("Viscoelastic fracture")
    in_folder = _ingest(library, make_pdf, "Anand", year=2023, collection_id=folder.id)
    loose = _ingest(library, make_pdf, "Rossi", year=2021)
    review = library.ingest_pdf(make_pdf("scan.pdf"), metadata=PaperMetadata()).paper.id
    library.update_flags(loose, favorite=True, my_publication=True)
    window.reload_tree()

    def shown(kind: NodeKind | None = None, cid: int | None = None) -> set[int]:
        if kind is not None:
            window.tree.select_node(kind, cid)
            QApplication.processEvents()
        return {window.proxy.index(r, 0).data(PAPER_ID_ROLE) for r in range(window.proxy.rowCount())}

    assert shown(NodeKind.ALL) == {in_folder, loose, review}
    assert shown(NodeKind.FOLDER, folder.id) == {in_folder}
    assert window.list_title.text() == "Viscoelastic fracture" and "Projects" in window.breadcrumb.text()
    assert "viscoelastic_fracture.bib" in window.list_subtitle.text()
    assert shown(NodeKind.INBOX) == {review} and window.verify_all_button.isVisibleTo(window)
    assert shown(NodeKind.SAVED, 1) == {loose}  # Authored by me
    assert shown(NodeKind.SAVED, 0) == set()  # No PDF yet
    assert window.list_title.text() == "No PDF yet"

    assert shown(NodeKind.ALL) == {in_folder, loose, review}
    window.filters.set_toggle("is:starred", True)
    assert shown() == {loose} and window.filters.clear_link.isVisibleTo(window)
    window.filters.set_toggle("is:starred", False)
    window.filters.set_toggle("-has:project", True)
    assert shown() == {loose, review}
    window.filters.clear()
    window.filters.set_year("2023", "year:2023")
    assert shown() == {in_folder} and window.filters.year_chip.text() == "Year: 2023"
    window.filters.clear()
    window.filters.set_facet("authors", "Rossi, J.")
    assert shown() == {loose} and window.filters.facet_chips["authors"].text() == "Author: Rossi, J."
    window.filters.clear()
    assert shown() == {in_folder, loose, review} and not window.filters.active()


def test_saved_search_from_the_search_box_and_chips(
    window: MainWindow, library: Library, make_pdf: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    a = _ingest(library, make_pdf, "Anand", year=2015)
    b = _ingest(library, make_pdf, "Anand", "Gurtin", year=2021)
    library.update_flags(b, favorite=True)
    window.reload_view()
    window.search.setText("author:anand")
    window._apply_search()
    window.filters.set_toggle("is:starred", True)
    monkeypatch.setattr(QInputDialog, "getText", lambda *args, **kw: ("Starred Anand", True))
    window.save_current_search()
    name, query, _sort = library.config.saved_searches[-1]
    assert (name, query) == ("Starred Anand", "author:anand is:starred")
    assert window.node == (NodeKind.SAVED, len(library.config.saved_searches) - 1)
    window.search.clear()
    window.filters.clear()
    window._apply_search()
    assert {window.proxy.index(r, 0).data(PAPER_ID_ROLE) for r in range(window.proxy.rowCount())} == {b}
    saved = json.loads(window.user_config.read_text(encoding="utf-8"))["saved_searches"]
    assert saved[-1] == {"name": "Starred Anand", "query": "author:anand is:starred", "sort": window.sort_key}
    window.remove_saved_search(len(library.config.saved_searches) - 1)
    assert all(n != "Starred Anand" for n, _q, _s in library.config.saved_searches)
    assert a in {p.id for p in library.papers()}


def test_rows_view_star_glyph_sort_menu_and_command_palette(
    window: MainWindow, library: Library, make_pdf: Callable[..., Path]
) -> None:
    old = _ingest(library, make_pdf, "Simo", year=1987)
    new = _ingest(library, make_pdf, "Anand", year=2023)
    window.reload_view()
    window.table.reference_delegate.starClicked.emit(old)
    assert library.get_paper(old).favorite
    window.set_sort("year")
    order = [window.proxy.index(r, 0).data(PAPER_ID_ROLE) for r in range(window.proxy.rowCount())]
    assert order == [new, old] and window.table.reference_delegate.year_gutter
    assert window.filters.sort_button.text() == "Sort: Year, newest first"
    window.set_sort("year_asc")
    assert [window.proxy.index(r, 0).data(PAPER_ID_ROLE) for r in range(window.proxy.rowCount())] == [old, new]
    window.set_view_mode("table")
    assert not window.table.reference_delegate.year_gutter and not window.table.horizontalHeader().isHidden()
    window.set_view_mode("rows")

    _select(window, new)
    window.search.setText(">copy bib")
    assert window.completer.completionCount() >= 1
    assert window.run_command("Copy BibTeX Entry") and window.search.text() == ""
    assert QGuiApplication.clipboard().text().startswith("@article{Anand_2023")
    assert not window.run_command("No such thing")


def test_reference_sheet_cite_block_and_project_chips(
    window: MainWindow, library: Library, make_pdf: Callable[..., Path]
) -> None:
    thesis = library.create_collection("PhD thesis")
    draft = library.create_collection("Draft")
    pid = _ingest(library, make_pdf, "Rossi", year=2021, title="Mullins effect in PINNs", collection_id=thesis.id)
    window.reload_tree(select=(NodeKind.FOLDER, thesis.id))
    _select(window, pid)
    details = window.inspector.details
    assert details.cite_code.text() == "\\cite{Rossi_2021}"
    details.cite_group.button(1).click()
    assert details.cite_code.text() == "\\citep{Rossi_2021}"
    details.copy_citation()
    assert QGuiApplication.clipboard().text() == "\\citep{Rossi_2021}"
    assert "{Mullins}" in details.bibtex_view.text() and "phd_thesis.bib" in details.bib_caption.text()
    assert [a.text().strip() for a in details.project_menu.actions() if a.isEnabled()][:1] == ["Draft"]

    window.inspector.projectToggled.emit(draft.id, True)
    assert {c.id for c in library.collections_of(pid)} == {thesis.id, draft.id}
    assert "Rossi_2021" in bib_entries(library.bib.collection_path(draft.slug))
    window.inspector.projectToggled.emit(thesis.id, False)
    assert [c.id for c in library.collections_of(pid)] == [draft.id]
    assert window.model.rowCount() == 0  # it left the project being shown
    assert window.list_subtitle.text().startswith("0 references")


def test_manuscript_check_compares_cited_keys_with_the_project(
    window: MainWindow, library: Library, make_pdf: Callable[..., Path], tmp_path: Path
) -> None:
    project = library.create_collection("Viscoelastic fracture")
    cited = _ingest(library, make_pdf, "Simo", year=1987, collection_id=project.id)
    uncited = _ingest(library, make_pdf, "Gent", year=1996, collection_id=project.id)
    elsewhere = _ingest(library, make_pdf, "Reese", "Govindjee", year=1998)
    manuscript = tmp_path / "paper"
    manuscript.mkdir()
    (manuscript / "main.tex").write_text(
        "% \\cite{Commented_2000}\n\\cite{Simo_1987} and \\citep[p.~3]{Reese_Govindjee_1998, Missing_2001}\n",
        encoding="utf-8",
    )
    library.set_collection_export_path(project.id, str(manuscript / "refs.bib"))
    window.reload_tree(select=(NodeKind.FOLDER, project.id))
    window.runner.wait_idle()
    QApplication.processEvents()
    check = window.manuscript
    assert check is not None and check.cited == {"Simo_1987", "Reese_Govindjee_1998", "Missing_2001"}
    assert check.missing == ["Missing_2001", "Reese_Govindjee_1998"] and check.missing_in_library == ["Reese_Govindjee_1998"]
    assert check.unused == ["Gent_1996"]
    assert window.header.check.isVisibleTo(window) and window.header.missing.number.text() == "2"
    assert "main.tex" in window.header.cited.text.text()

    window.show_uncited()
    assert {window.proxy.index(r, 0).data(PAPER_ID_ROLE) for r in range(window.proxy.rowCount())} == {uncited}
    assert window.filters.restrict_chip.isVisibleTo(window)
    window.filters.restrict_chip.click()
    assert window.proxy.rowCount() == 2

    window.add_missing_citations()
    window.runner.wait_idle()
    QApplication.processEvents()
    assert {p.id for p in library.papers(project.id)} == {cited, uncited, elsewhere}
    assert window.manuscript is not None and window.manuscript.missing == ["Missing_2001"]


def test_inbox_triage_marks_verified_and_moves_to_the_next(
    window: MainWindow, library: Library, make_pdf: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    first = library.ingest_pdf(make_pdf("a.pdf"), metadata=meta("Anand", year=2020, doi="10.1/abc")).paper.id
    second = library.ingest_pdf(make_pdf("b.pdf"), metadata=meta("Rossi", year=2021)).paper.id
    third = library.ingest_pdf(make_pdf("c.pdf"), metadata=meta("Bianchi", year=2022)).paper.id
    for pid in (first, second, third):
        library.update_metadata(pid, needs_review=True)
    window.reload_tree(select=(NodeKind.INBOX, None))
    window.set_sort("year_asc")
    _select(window, first)
    assert not window.inspector.details.verify.isHidden()
    assert "DOI" in window.inspector.details.verify_text.text()
    window.act_verify.trigger()
    assert not library.get_paper(first).needs_review
    assert window.selected_ids() == [second] and window.model.rowCount() == 2
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    library.update_metadata(third, doi="10.1/xyz")
    window.verify_all_with_doi()
    assert [p.id for p in library.papers_needing_review()] == [second]


def test_study_mode_shares_the_notes_with_the_sheet(
    window: MainWindow, library: Library, make_pdf: Callable[..., Path]
) -> None:
    pid = _ingest(library, make_pdf, "Reese", "Govindjee", year=1998)
    window.reload_view()
    _select(window, pid)
    window.act_study.trigger()
    assert window.pages.currentIndex() == 1 and window.study.paper is not None and window.study.paper.id == pid
    assert window.study.key_badge.text() == "Reese_Govindjee_1998"
    window.study.editor.setPlainText("Evolution law valid far from equilibrium.")
    window.study.flush_notes()
    assert library.get_paper(pid).notes == "Evolution law valid far from equilibrium."
    window.exit_study()
    assert window.pages.currentIndex() == 0
    assert window.inspector.notes.toPlainText() == "Evolution law valid far from equilibrium."


# =========================================================================== drag & drop
def test_dragging_documents_onto_a_folder_links_them_without_copying(
    window: MainWindow, library: Library, make_pdf: Callable[..., Path]
) -> None:
    pid = _ingest(library, make_pdf, "Anand", year=2023)
    folder = library.create_collection("PINN constitutive")
    window.reload_tree()

    mime = window.model.mimeData([window.model.index(window.model.row_of(pid) or 0, 0)])
    assert mime.text() == "\\cite{Anand_2023}"  # dropping into a .tex editor pastes the citation
    target = window.tree.find_index(NodeKind.FOLDER, folder.id)
    model = window.tree.source
    assert model.canDropMimeData(mime, Qt.DropAction.CopyAction, -1, -1, target)
    model.dropMimeData(mime, Qt.DropAction.CopyAction, -1, -1, target)

    assert [p.id for p in library.papers(folder.id)] == [pid]
    assert pdfs_on_disk(library.root) == ["pdfs/2023/Anand_2023.pdf"]
    assert "Anand_2023" in bib_entries(library.bib.collection_path(folder.slug))

    assert not model.canDropMimeData(mime, Qt.DropAction.CopyAction, -1, -1, window.tree.find_index(NodeKind.INBOX))
    assert not model.canDropMimeData(mime, Qt.DropAction.CopyAction, -1, -1, window.tree.find_index(NodeKind.ALL))


def test_dropping_pdf_files_imports_them_in_the_background(
    qapp: QApplication, config: AppConfig, tmp_path: Path, make_pdf: Callable[..., Path], launched: list[str]
) -> None:
    seen: list[httpx.Request] = []
    library = Library(config, resolver=api_resolver(config, seen, crossref={TEST_DOI: CROSSREF_MESSAGE}))
    folder = library.create_collection("Hydrogel fracture")
    win = _make_window(qapp, library, tmp_path / "gui.ini")
    try:
        pdf = make_pdf(
            "download.pdf", title=CROSSREF_MESSAGE["title"][0], lines=[f"https://doi.org/{TEST_DOI}", "M. Rossi"]
        )
        copy = Path(shutil.copy2(pdf, pdf.with_name("same file again.pdf")))
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(str(pdf))])
        target = win.tree.find_index(NodeKind.FOLDER, folder.id)
        assert win.tree.source.canDropMimeData(mime, Qt.DropAction.CopyAction, -1, -1, target)
        win.tree.source.dropMimeData(mime, Qt.DropAction.CopyAction, -1, -1, target)
        assert win.importer.pending == 1
        win.runner.wait_idle()

        assert [p.citation_key for p in library.papers(folder.id)] == ["Rossi_Bianchi_2023"]
        assert not pdf.exists()
        assert pdfs_on_disk(library.root) == ["pdfs/2023/Rossi_Bianchi_2023.pdf"]
        assert win.status_label.text() == "Import finished: 1 imported"

        win.import_paths([copy], folder.id)  # same bytes: detected in the worker, no HTTP
        requests_before = len(seen)
        win.runner.wait_idle()
        assert len(seen) == requests_before
        assert win.status_label.text() == "Import finished: 0 imported, 1 already in library"
        assert copy.exists()

        win.tree.select_node(NodeKind.FOLDER, folder.id)
        qapp.processEvents()
        assert win.model.rowCount() == 1
    finally:
        win.runner.wait_idle()
        win.close()
        library.close()


# =========================================================================== inspector
def test_inspector_edits_rename_the_pdf_and_rewrite_the_bib(
    window: MainWindow, library: Library, make_pdf: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = library.create_collection("Draft")
    pid = _ingest(library, make_pdf, "Anand", year=2023, collection_id=folder.id)
    window.reload_tree(select=(NodeKind.FOLDER, folder.id))
    _select(window, pid)
    details = window.inspector.details
    assert details.citation_key.text() == "Anand_2023"

    details.year.setText("2024")
    details.year.editingFinished.emit()
    assert library.get_paper(pid).relative_pdf_path == "pdfs/2024/Anand_2024.pdf"
    assert pdfs_on_disk(library.root) == ["pdfs/2024/Anand_2024.pdf"]
    assert details.citation_key.text() == "Anand_2024"
    assert window.model.data(window.model.index(0, int(Col.YEAR))) == "2024"
    assert "Anand_2024.pdf" in window.status_label.text()

    details.title.setPlainText("Cauchy stress in PINNs")
    details.title._commit()
    assert bib_entries(library.bib.collection_path(folder.slug))["Anand_2024"]["title"] == "{Cauchy} stress in {PINNs}"

    details.tags.setPlainText("rubber;fracture")
    details.tags._commit()
    assert library.get_paper(pid).tags == "rubber; fracture"

    warnings: list[str] = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: warnings.append(str(args[2])))
    details.year.setText("not a year")
    details.year.editingFinished.emit()
    assert warnings and library.get_paper(pid).meta.year == 2024
    assert details.year.text() == "2024"  # reverted to the stored value

    window.inspector.notes.setPlainText("Check eq. (12) against the FE results.")
    window.inspector.flush_notes()
    assert library.get_paper(pid).notes == "Check eq. (12) against the FE results."


def test_verify_card_disappears_once_the_details_are_fixed(
    window: MainWindow, library: Library, make_pdf: Callable[..., Path]
) -> None:
    pid = library.ingest_pdf(make_pdf("scan.pdf"), metadata=PaperMetadata(title="Unknown scan")).paper.id
    window.reload_view()
    _select(window, pid)
    assert not window.inspector.details.verify.isHidden()
    window.inspector.details.authors.setPlainText("Lancioni, Giovanni")
    window.inspector.details.authors._commit()
    window.inspector.details.year.setText("2025")
    window.inspector.details.year.editingFinished.emit()
    paper = library.get_paper(pid)
    assert paper.citation_key == "Lancioni_2025" and not paper.needs_review
    assert window.inspector.details.verify.isHidden()


def test_contents_tab_shows_the_pdf_outline(window: MainWindow, library: Library, tmp_path: Path) -> None:
    doc = pymupdf.open()
    for _ in range(3):
        doc.new_page()
    doc.set_toc([[1, "1 Introduction", 1], [1, "2 Kinematics", 2], [2, "2.1 Deformation gradient", 2]])
    source = tmp_path / "with_toc.pdf"
    doc.save(source)
    doc.close()
    pid = library.ingest_pdf(source, metadata=meta("Anand", year=2023)).paper.id
    window.reload_view()
    _select(window, pid)
    window.runner.wait_idle()
    assert window.inspector.outline.topLevelItemCount() == 2
    assert "3 pages" in window.inspector.contents_label.text()


# =========================================================================== table actions
def test_star_and_read_columns_toggle_flags(
    window: MainWindow, library: Library, make_pdf: Callable[..., Path]
) -> None:
    pid = _ingest(library, make_pdf, "Anand", year=2023)
    window.reload_view()
    window._on_table_clicked(_proxy_index(window, pid, Col.FAVORITE))
    window._on_table_clicked(_proxy_index(window, pid, Col.READ))
    paper = library.get_paper(pid)
    assert paper.favorite and paper.is_read
    window._on_table_clicked(_proxy_index(window, pid, Col.FAVORITE))
    assert not library.get_paper(pid).favorite


def test_open_copy_and_delete(
    window: MainWindow,
    library: Library,
    make_pdf: Callable[..., Path],
    launched: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    a = _ingest(library, make_pdf, "Anand", year=2023)
    b = _ingest(library, make_pdf, "Rossi", year=2021)
    window.reload_view()
    _select(window, a, b)
    ordered = [window.proxy.index(r, 0).data(PAPER_ID_ROLE) for r in range(window.proxy.rowCount())]
    keys = [library.get_paper(pid).citation_key for pid in ordered]

    window.act_copy_cite.trigger()
    assert QGuiApplication.clipboard().text() == "\\cite{" + ",".join(keys) + "}"
    window.act_copy_bibtex.trigger()
    assert QGuiApplication.clipboard().text().count("@article{") == 2

    _select(window, a)
    window.act_open.trigger()
    assert [Path(p) for p in launched] == [library.pdf_path(library.get_paper(a))]
    assert library.get_paper(a).is_read
    assert [p.id for p in library.recently_read()] == [a]

    monkeypatch.setattr(QMessageBox, "question", lambda *args, **kw: QMessageBox.StandardButton.Yes)
    _select(window, a, b)
    window.act_delete.trigger()
    assert library.db.count_papers() == 0
    assert pdfs_on_disk(library.root) == []
    assert window.model.rowCount() == 0


def test_metadata_search_fulltext_search_and_author_filter(
    window: MainWindow, library: Library, make_pdf: Callable[..., Path]
) -> None:
    rubber = _ingest(library, make_pdf, "Rossi", year=2021, title="Stress softening", lines=["The Mullins effect."])
    bridge = _ingest(library, make_pdf, "Bianchi", year=2020, title="Bridge monitoring", lines=["Modal analysis."])
    window.reload_view()

    window.search.setText("mullins")
    window._apply_search()
    assert window.proxy.rowCount() == 0  # body text is not document metadata
    window.set_search_mode(SEARCH_FULLTEXT)
    assert [window.proxy.index(r, 0).data(PAPER_ID_ROLE) for r in range(window.proxy.rowCount())] == [rubber]
    assert window.count_label.text() == "1 of 2 references"

    window.search.clear()
    window._apply_search()
    window.filters.set_facet("authors", "Bianchi, J.")
    assert [window.proxy.index(r, 0).data(PAPER_ID_ROLE) for r in range(window.proxy.rowCount())] == [bridge]


# =========================================================================== folders & settings
def test_folder_lifecycle_from_the_window(
    window: MainWindow, library: Library, make_pdf: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(QInputDialog, "getText", lambda *args, **kw: ("Draft paper", True))
    window.new_folder()
    (collection,) = library.collections()
    assert collection.slug == "draft_paper"
    assert window.node == (NodeKind.FOLDER, collection.id)

    item = window.tree.source.itemFromIndex(window.tree.find_index(NodeKind.FOLDER, collection.id))
    item.setText("SHM bridges")  # in-place rename (F2)
    QApplication.processEvents()
    assert library.collections()[0].slug == "shm_bridges"
    assert (library.root / "bibtex" / "shm_bridges.bib").exists()

    monkeypatch.setattr(QMessageBox, "question", lambda *args, **kw: QMessageBox.StandardButton.Yes)
    window.remove_folder(collection.id)
    assert library.collections() == []
    assert window.node == (NodeKind.ALL, None)


def test_external_bib_target_asks_before_replacing_a_foreign_file(
    window: MainWindow, library: Library, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    collection = library.create_collection("Paper draft")
    foreign = tmp_path / "references.bib"
    foreign.write_text("@book{knuth1984, title = {The TeXbook}}\n", encoding="utf-8")
    answers = iter([QMessageBox.StandardButton.No, QMessageBox.StandardButton.Yes])
    monkeypatch.setattr(QMessageBox, "question", lambda *args, **kw: next(answers))

    window.set_export_path(collection, str(foreign))
    assert foreign.read_text(encoding="utf-8").startswith("@book{knuth1984")
    window.set_export_path(collection, str(foreign))
    assert foreign.read_text(encoding="utf-8").startswith("% Generated by JRef")


def test_layout_settings_persist_between_sessions(
    qapp: QApplication, library: Library, tmp_path: Path, launched: list[str]
) -> None:
    collection = library.create_collection("Viscoelastic fracture")
    settings = tmp_path / "persist.ini"
    first = _make_window(qapp, library, settings)
    first.reload_tree(select=(NodeKind.FOLDER, collection.id))
    first.set_view_mode("table")
    first.set_sort("title")
    first.table.horizontalHeader().setSectionHidden(int(Col.PUBLISHED_IN), True)
    first.table.horizontalHeader().setSectionHidden(int(Col.CITATION_KEY), False)
    first.set_search_mode(SEARCH_FULLTEXT)
    first.close()

    second = _make_window(qapp, library, settings)
    try:
        header = second.table.horizontalHeader()
        assert header.isSectionHidden(int(Col.PUBLISHED_IN))
        assert not header.isSectionHidden(int(Col.CITATION_KEY))
        assert second.node == (NodeKind.FOLDER, collection.id)
        assert second.search_mode == SEARCH_FULLTEXT
        assert second.view_mode == "table" and second.sort_key == "title"
    finally:
        second.close()


def test_author_inverted_names_round_trip_through_the_editor() -> None:
    m = PaperMetadata(authors=[Author("van der Giessen", "Erik"), Author("Rossi", "Mario")])
    assert m.authors_inverted() == "van der Giessen, Erik; Rossi, Mario"


# =========================================================================== modern UI: theme, counts, preferences
def test_dark_theme_switch_restyles_and_persists_choice(window: MainWindow, qapp: QApplication) -> None:
    window.apply_theme("dark")
    assert theme.current().dark
    assert theme.DARK.window in qapp.styleSheet()
    assert window.theme_mode == "dark"
    window.apply_theme("light")
    assert not theme.current().dark and theme.LIGHT.window in qapp.styleSheet()


def test_navigation_shows_counts(window: MainWindow, library: Library, make_pdf: Callable[..., Path]) -> None:
    from bibmanager.gui.sidebar import COUNT_ROLE

    folder = library.create_collection("Draft")
    _ingest(library, make_pdf, "Anand", year=2023, collection_id=folder.id)
    library.ingest_pdf(make_pdf("scan.pdf"), metadata=PaperMetadata())
    window.reload_tree()
    assert window.tree.find_index(NodeKind.ALL).data(COUNT_ROLE) == 2
    assert window.tree.find_index(NodeKind.INBOX).data(COUNT_ROLE) == 1
    assert window.tree.find_index(NodeKind.FOLDER, folder.id).data(COUNT_ROLE) == 1
    assert window.table.empty_state.isHidden()


def test_preferences_are_saved_and_applied(
    window: MainWindow, library: Library, make_pdf: Callable[..., Path], tmp_path: Path
) -> None:
    from bibmanager.gui.dialogs import PreferenceValues

    folder = library.create_collection("Draft")
    _ingest(library, make_pdf, "Anand", year=2023, title="Duffing oscillators", collection_id=folder.id)
    switched: list[AppConfig] = []
    window.libraryChangeRequested.connect(switched.append)

    window.apply_preferences(
        PreferenceValues(
            library_root=library.root,
            contact_email="someone@example.org",
            import_mode="copy",
            theme_mode="light",
            extra_protected_words=("Duffing",),
        )
    )
    assert window.user_config.is_file()
    assert library.config.import_mode == "copy" and library.config.user_agent.endswith("someone@example.org)")
    assert bib_entries(library.bib.collection_path(folder.slug))["Anand_2023"]["title"] == "{Duffing} oscillators"
    assert switched == []

    window.apply_preferences(
        PreferenceValues(
            library_root=tmp_path / "another library",
            contact_email="someone@example.org",
            import_mode="copy",
            theme_mode="light",
            extra_protected_words=("Duffing",),
        )
    )
    assert len(switched) == 1 and switched[0].library_root == tmp_path / "another library"


def test_first_run_dialog_collects_library_settings(qapp: QApplication, config: AppConfig, tmp_path: Path) -> None:
    from PySide6.QtWidgets import QDialogButtonBox

    from bibmanager.gui.dialogs import PreferencesDialog

    dialog = PreferencesDialog(None, config=config, theme_mode="system", first_run=True)
    ok = dialog.findChild(QDialogButtonBox).buttons()[0]
    assert ok.text() == "Get started" and ok.objectName() == "primary"
    dialog.root.setText(str(tmp_path / "Library"))
    dialog.email.setText("someone@example.org")
    dialog.copy_radio.setChecked(True)
    values = dialog.values()
    assert values.library_root == tmp_path / "Library"
    assert values.contact_email == "someone@example.org" and values.import_mode == "copy"
    dialog.deleteLater()


# =========================================================================== v0.4: Mendeley, inbox, naming
def test_mendeley_import_runs_in_the_background_and_takes_over_bib_files(
    window: MainWindow, library: Library, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, launched: list[str]
) -> None:
    from bibmanager.core.mendeley import MendeleyImportOptions

    from .test_import import write_pdf

    pdfs = tmp_path / "Mendeley Desktop"
    write_pdf(pdfs / "Rossi - 2021 - Rubber.pdf", "rubber")
    entry = (
        "@article{Rossi2021,\nauthor = {Rossi, Mario},\njournal = {J. Rubber},\n"
        "file = {:C$\\backslash$:/Users/old/Mendeley Desktop/Rossi - 2021 - Rubber.pdf:pdf},\n"
        "title = {{Rubber {\\&} friends}},\nyear = {2021}\n}\n"
        "@book{Knuth1984,\nauthor = {Knuth, Donald},\ntitle = {{The {\\TeX}book}},\nyear = {1984}\n}\n"
    )
    manuscript = tmp_path / "manuscript"
    manuscript.mkdir()
    thesis_bib = manuscript / "Thesis.bib"
    thesis_bib.write_text("Automatically generated by Mendeley Desktop 1.19.8\n\n" + entry, encoding="utf-8")
    reports: list[Any] = []
    monkeypatch.setattr("bibmanager.gui.main_window.show_import_report", lambda parent, report: reports.append(report))

    window.mendeley.start(
        MendeleyImportOptions(bib_paths=[thesis_bib], pdf_folder=pdfs, bib_as_folder=True, take_over_bib_files=True)
    )
    window.runner.wait_idle()
    (report,) = reports
    assert (report.total, report.imported, report.linked, report.without_pdf) == (2, 2, 1, 1)
    assert not window.mendeley.running and library._bulk_depth == 0

    (folder,) = library.collections()
    assert folder.name == "Thesis" and folder.bib_export_path == str(thesis_bib)
    assert set(bib_entries(thesis_bib)) == {"Knuth1984", "Rossi2021"}  # manuscript keeps compiling
    assert thesis_bib.read_text(encoding="utf-8").startswith("% Generated by JRef")
    assert window.tree.find_index(NodeKind.FOLDER, folder.id).isValid()

    rossi = library.db.get_paper_by_citation_key("Rossi2021")
    knuth = library.db.get_paper_by_citation_key("Knuth1984")
    assert rossi is not None and knuth is not None
    _select(window, knuth.id)
    assert "Attach PDF" in window.inspector.details.file_link.text()
    _select(window, rossi.id)
    assert "(linked)" in window.inspector.details.file_link.text()
    window.act_open.trigger()
    assert [Path(p) for p in launched] == [pdfs / "Rossi - 2021 - Rubber.pdf"]

    window.store_linked([rossi.id])
    stored = library.get_paper(rossi.id)
    assert stored.pdf_managed and (library.root / "pdfs/2021/Rossi2021.pdf").is_file()


def test_apply_naming_rule_from_the_tools_menu(
    window: MainWindow, library: Library, make_pdf: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    pid = _ingest(library, make_pdf, "Anand", year=2023)
    library.update_metadata(pid, citation_key="anand_old")
    asked: list[int] = []
    monkeypatch.setattr(
        "bibmanager.gui.main_window.confirm_naming_rule", lambda parent, outdated: asked.append(len(outdated)) or True
    )
    window.act_apply_naming.trigger()
    assert asked == [1]
    paper = library.get_paper(pid)
    assert (paper.citation_key, paper.relative_pdf_path) == ("Anand_2023", "pdfs/2023/Anand_2023.pdf")


def test_inbox_watcher_imports_new_pdfs(
    qapp: QApplication, library: Library, tmp_path: Path, make_pdf: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    import time

    from bibmanager.core.extractor import Resolution

    monkeypatch.setattr(MainWindow, "INBOX_SETTLE_S", 0.2)
    monkeypatch.setattr(
        library.resolver, "resolve", lambda content: Resolution(meta("Inbox", year=2024), "test", needs_review=False)
    )
    win = _make_window(qapp, library, tmp_path / "inbox-gui.ini", watch_inbox=True)
    try:
        assert win.inbox is not None and win.inbox.running
        inbox = library.config.inbox_path
        source = make_pdf("downloaded.pdf", title="Inbox paper")
        duplicate = tmp_path / "copy.pdf"
        shutil.copy2(source, duplicate)
        shutil.move(str(source), str(inbox / "downloaded.pdf"))
        deadline = time.monotonic() + 15
        while library.db.count_papers() == 0 and time.monotonic() < deadline:
            qapp.processEvents()
            time.sleep(0.05)
        win.runner.wait_idle()
        assert library.db.count_papers() == 1 and not (inbox / "downloaded.pdf").exists()
        assert (library.root / "pdfs/2024/Inbox_2024.pdf").is_file()

        shutil.move(str(duplicate), str(inbox / "again.pdf"))
        deadline = time.monotonic() + 15
        while not (inbox / "Duplicates" / "again.pdf").exists() and time.monotonic() < deadline:
            qapp.processEvents()
            win.runner.wait_idle()
            time.sleep(0.05)
        assert (inbox / "Duplicates" / "again.pdf").exists() and library.db.count_papers() == 1
    finally:
        win.runner.wait_idle()
        win.close()
        win.deleteLater()
    assert win.inbox is None


def test_mendeley_dialog_builds_import_options(qapp: QApplication, tmp_path: Path) -> None:
    from bibmanager.gui.dialogs import MendeleyImportDialog

    entry = "@article{A2020,\nauthor = {A, B},\ntitle = {{T}},\nyear = {2020},\nfile = {:x/a.pdf:pdf}\n}\n"
    library_bib = tmp_path / "library.bib"
    library_bib.write_text(entry, encoding="utf-8")
    thesis = tmp_path / "Thesis.bib"
    thesis.write_text(entry.replace("A2020", "B2021"), encoding="utf-8")
    dialog = MendeleyImportDialog(None, start_dir=tmp_path)
    dialog.add_bib_files([library_bib, thesis])
    # several files = Mendeley's one-file-per-folder export: ticked by default, except a whole-library file
    assert dialog.bib_folders() == {library_bib: "", thesis: "Thesis"}
    assert dialog.table.item(1, dialog.COL_ENTRIES).text() == "1"
    assert dialog.table.item(1, dialog.COL_FILES).text() == "1"
    assert "none in a Mendeley folder" not in dialog.hint.text() and "1 project(s) from files" in dialog.hint.text()
    dialog.table.item(1, dialog.COL_FOLDER).setText("PhD thesis")
    dialog.pdf_folder.setText(str(tmp_path))
    dialog.database.setText("")
    dialog.file_mode.setCurrentIndex(dialog.file_mode.findData("copy"))
    dialog.key_mode.setCurrentIndex(1)
    dialog.take_over.setChecked(True)
    options = dialog.options()
    assert options.bib_paths == [library_bib, thesis] and options.pdf_folder == tmp_path and options.database is None
    assert options.bib_folders == {library_bib: "", thesis: "PhD thesis"}
    assert (options.file_mode, options.keep_keys, options.take_over_bib_files) == ("copy", False, True)
    dialog.table.selectRow(0)
    dialog._remove_selected()
    assert dialog.bib_paths() == [thesis]
    dialog.deleteLater()


def test_brand_icon_and_settings_migration(qapp: QApplication, tmp_path: Path) -> None:
    from bibmanager import APP_NAME
    from bibmanager.gui import icons
    from bibmanager.gui.app import migrate_legacy_qsettings

    icon = icons.app_icon()
    assert not icon.isNull()
    assert {s.width() for s in icon.availableSizes()} >= {16, 24, 32, 48, 256}
    assert not icons.brand_pixmap(96).isNull() and not icons.brand_pixmap(64, with_name=False).isNull()

    legacy = QSettings(str(tmp_path / "old.ini"), QSettings.Format.IniFormat)
    legacy.setValue("view/theme", "dark")
    legacy.setValue("window/geometry", b"\x01\x02")
    target = QSettings(str(tmp_path / "new.ini"), QSettings.Format.IniFormat)
    assert migrate_legacy_qsettings(target, legacy) == 2
    assert target.value("view/theme") == "dark"
    target.setValue("view/theme", "light")
    assert migrate_legacy_qsettings(target, legacy) == 0 and target.value("view/theme") == "light"
    assert APP_NAME == "JRef"


def test_window_title_and_about_use_the_new_name(window: MainWindow, monkeypatch: pytest.MonkeyPatch) -> None:
    assert window.windowTitle() == "JRef \u2014 Journal Reference Manager"
    shown: list[str] = []
    monkeypatch.setattr(QMessageBox, "exec", lambda box: shown.append(box.windowTitle() + "|" + box.text()) or 0)
    window.show_help()
    assert shown and shown[0].startswith("About JRef|<h3>JRef 0.8.0</h3><p><b>Journal Reference Manager</b>")
    assert "Author: JC" in shown[0] and "j.c@outlook.com" in shown[0]


def test_attach_by_dropping_a_pdf_on_a_row_and_find_missing(
    window: MainWindow, library: Library, make_pdf: Callable[..., Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from bibmanager.core import ReferenceCandidate

    ref = library.commit_reference(ReferenceCandidate("Gu2013", meta("Gu", year=2013, title="Surface wrinkling"))).paper
    other = library.commit_reference(ReferenceCandidate("Cao2012", meta("Cao", year=2012, title="Buckling"))).paper
    window.reload_view()
    row = next(r for r in range(window.proxy.rowCount()) if window.proxy.index(r, 0).data(PAPER_ID_ROLE) == ref.id)
    window._on_files_dropped([str(make_pdf("gu.pdf"))], row)
    window.runner.wait_idle()
    assert library.get_paper(ref.id).has_pdf and library.get_paper(ref.id).relative_pdf_path == "pdfs/2013/Gu2013.pdf"

    folder = tmp_path / "Mendeley Desktop"
    folder.mkdir()
    shutil.copy2(make_pdf("cao.pdf"), folder / "Cao et al. - 2012 - Buckling.pdf")
    reports: list[Any] = []
    monkeypatch.setattr("bibmanager.gui.main_window.show_attach_report", lambda *a, **k: reports.append(a[1]))
    monkeypatch.setattr(
        "bibmanager.gui.main_window.FindPdfsDialog.exec", lambda self: FindPdfsDialog_accept(self, folder)
    )
    window.find_missing_pdfs()
    window.runner.wait_idle()
    (report,) = reports
    assert report.attached == ["Cao2012"] and report.rules == {"title": 1}
    cao = library.get_paper(other.id)
    assert cao.pdf_linked and library.pdf_path(cao) == folder / "Cao et al. - 2012 - Buckling.pdf"


def FindPdfsDialog_accept(dialog: Any, folder: Path) -> int:  # noqa: N802 - test helper standing in for exec()
    dialog.folder.setText(str(folder))
    return 1


def test_field_qualifiers_and_search_scope(window: MainWindow, library: Library, make_pdf: Callable[..., Path]) -> None:
    from bibmanager.gui.main_window import SEARCH_METADATA

    rossi = _ingest(library, make_pdf, "Rossi", year=2021, title="Anand model of rubber", lines=["Mullins effect"])
    anand = _ingest(library, make_pdf, "Anand", year=2015, title="Gradient plasticity")
    library.update_metadata(anand, journal="J. Mech. Phys. Solids")
    library.update_flags(rossi, tags="review")
    window.reload_view()

    def shown() -> set[int]:
        return {window.proxy.index(r, 0).data(PAPER_ID_ROLE) for r in range(window.proxy.rowCount())}

    def search(text: str) -> set[int]:
        window.search.setText(text)
        window._apply_search()
        return shown()

    window.set_search_mode(SEARCH_METADATA)
    assert search("anand") == {rossi, anand}  # all fields: the title of Rossi mentions Anand
    assert search("author:anand") == {anand}
    assert search("journal:solids") == {anand}
    assert search("year:2016-2025") == {rossi}
    assert search("tag:review") == {rossi} and search("-tag:review") == {anand}
    window.scope_actions["author"].trigger()  # Search in: Authors
    assert window.search_scope == "author" and "authors" in window.search.placeholderText()
    assert search("anand") == {anand}
    window.act_search_full.trigger()  # full text + qualifier
    assert search("mullins") == {rossi}
    assert search("mullins year:<2020") == set()
    window.scope_actions["all"].trigger()
    assert search("") == {rossi, anand}


def test_search_for_author_from_the_context_menu(
    window: MainWindow, library: Library, make_pdf: Callable[..., Path]
) -> None:
    from PySide6.QtWidgets import QMenu

    a = _ingest(library, make_pdf, "Anand", year=2015)
    b = _ingest(library, make_pdf, "Rossi", year=2021)
    window.reload_view()
    menu = QMenu(window)
    window._fill_search_menu(menu, library.get_paper(a))
    labels = [action.text() for action in menu.actions()]
    assert labels[0].startswith("Author: ") and "Year: 2015" in labels
    menu.actions()[0].trigger()
    assert window.search.text() == "author:Anand"
    assert {window.proxy.index(r, 0).data(PAPER_ID_ROLE) for r in range(window.proxy.rowCount())} == {a}
    assert b in {p.id for p in library.papers()}


# =========================================================================== sharing between devices (v0.6)
def test_sharing_dialog_moves_the_library_to_the_pdf_folder_and_changes_of_a_phone_show_up(
    qapp: QApplication,
    config: AppConfig,
    make_pdf: Callable[..., Path],
    tmp_path: Path,
    launched: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import dataclasses

    from bibmanager.core import ReferenceCandidate
    from bibmanager.gui.dialogs import DeviceSyncDialog, DeviceSyncValues

    data = tmp_path / "data-desk"
    plain = dataclasses.replace(config, local_data_dir=data, import_mode="copy")
    library = Library(plain)
    papers_dir = tmp_path / "Dropbox" / "Papers"
    papers_dir.mkdir(parents=True)
    linked = Path(shutil.copy2(make_pdf("gent.pdf"), papers_dir / "Gent - 1996 - A new constitutive relation.pdf"))
    ref = library.commit_reference(ReferenceCandidate("Gent1996", meta("Gent", year=1996))).paper
    library.attach_pdf(ref.id, library.prepare_attachment(linked), mode="link")
    window = _make_window(qapp, library, tmp_path / "gui.ini")
    assert window.device_label.text() == "Not shared with other devices" and not window.act_sync_devices.isEnabled()

    dialog = DeviceSyncDialog(window, library)
    assert "1 linked PDFs are outside the library folder" in dialog.hint.text()
    suggestion = dialog.suggestions.itemAt(0).widget()
    suggestion.click()
    assert dialog.values().folder == papers_dir  # the folder where the PDFs are is one click away
    dialog.deleteLater()

    requested: list[AppConfig] = []
    window.libraryChangeRequested.connect(requested.append)
    monkeypatch.setattr(DeviceSyncDialog, "exec", lambda self: DeviceSyncDialog.DialogCode.Accepted)
    monkeypatch.setattr(DeviceSyncDialog, "values", lambda self: DeviceSyncValues(papers_dir, "Office PC"))
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    window.device_sync_dialog()
    assert len(requested) == 1 and requested[0].library_root == papers_dir and requested[0].sync_enabled
    saved = json.loads((tmp_path / "user-config.json").read_text(encoding="utf-8"))
    assert saved["sync"] == {"enabled": True, "device_name": "Office PC"} and saved["library_root"] == str(papers_dir)
    window.close()
    window.deleteLater()

    shared = Library(requested[0])
    assert shared.shared and linked.is_file() and shared.pdf_path(shared.get_paper(ref.id)) == linked
    assert shared.get_paper(ref.id).relative_pdf_path == linked.name  # relative: works on every device
    window = _make_window(qapp, shared, tmp_path / "gui2.ini")
    window.sync_devices(manual=True)
    assert "Shared · 0 other devices" in window.device_label.text() and window.act_sync_devices.isEnabled()

    phone_config = dataclasses.replace(
        requested[0], local_data_dir=tmp_path / "data-phone", device_name="Phone", write_bib_files=False,
        adopt_folder_database=False,
    )  # fmt: skip
    with Library(phone_config) as phone:
        phone.sync_now()
        assert [p.citation_key for p in phone.papers()] == ["Gent1996"] and phone.pdf_available(phone.papers()[0])
        folder = phone.create_collection("From the phone")
        phone.add_reference(meta("Arruda", "Boyce", year=1993, title="A three-dimensional model"), folder.id)
        phone.update_flags(phone.papers()[1].id, favorite=True)
        phone.sync_now()

    window.sync_devices()
    assert window.model.rowCount() == 2 and "received" in window.status_label.text()
    assert [c.name for c in shared.collections()] == ["From the phone"]
    assert "Shared · 1 other device ·" in window.device_label.text()
    assert "Arruda_Boyce_1993" in (papers_dir / "bibtex" / "from_the_phone.bib").read_text(encoding="utf-8")
    window.runner.wait_idle()
    window.close()
    window.deleteLater()
    shared.close()


# =========================================================================== supplementary files (v0.7)
def test_supplementary_files_by_drop_inspector_and_menu(
    window: MainWindow, library: Library, make_pdf: Callable[..., Path], tmp_path: Path, launched: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # fmt: skip
    from bibmanager.gui import icons as icon_module

    pid = _ingest(library, make_pdf, "Bergstrom", "Boyce", year=1998)
    window.reload_view()
    row = next(r for r in range(window.proxy.rowCount()) if window.proxy.index(r, 0).data(PAPER_ID_ROLE) == pid)

    data = tmp_path / "Movie S1.mp4"
    data.write_bytes(b"\x00\x00\x00\x18ftypmp42 fake video")
    asked: list[int] = []
    monkeypatch.setattr(MainWindow, "_ask_drop_target", lambda self, paper, files: asked.append(len(files)) or "supplement")
    window._on_files_dropped([str(data)], row)  # not a PDF: attached without asking
    window.runner.wait_idle()
    assert asked == [] and library.get_paper(pid).attachments == 1
    si = make_pdf("mmc2.pdf", lines=["supplementary experiments"])
    window._on_files_dropped([str(si)], row)  # a PDF on a document that has one: asked, then attached
    window.runner.wait_idle()
    assert asked == [1]
    attachments = library.attachments(pid)
    assert [a.file_name for a in attachments] == ["Bergstrom_Boyce_1998_S1.mp4", "Bergstrom_Boyce_1998_S2.pdf"]

    window.select_ids([pid])
    details = window.inspector.details
    assert not details.supplements.isHidden() and len(details._attachment_rows) == 2
    index = window.proxy.index(row, int(Col.FILE))
    assert "+ 2 supplementary files" in index.data(Qt.ItemDataRole.ToolTipRole)
    assert index.data(Qt.ItemDataRole.DecorationRole).cacheKey() == icon_module.pdf_with_supplements().cacheKey()

    details.attachmentAction.emit("open", attachments[0].id)
    assert Path(launched[-1]) == library.attachment_path(attachments[0])
    monkeypatch.setattr(QInputDialog, "getText", lambda *a, **k: ("Movie of the test", True))
    details.attachmentAction.emit("rename", attachments[0].id)
    assert library.get_attachment(attachments[0].id).label == "Movie of the test"
    main_before = library.get_paper(pid).sha256
    details.attachmentAction.emit("promote", attachments[1].id)
    assert library.get_paper(pid).sha256 == attachments[1].sha256
    assert library.get_attachment(attachments[1].id).sha256 == main_before
    assert library.search_fulltext("supplementary experiments")

    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    details.attachmentAction.emit("remove", attachments[0].id)
    window.runner.wait_idle()
    assert library.get_paper(pid).attachments == 1 and not library.attachment_path(attachments[0]).exists()
    assert len(window.inspector.details._attachment_rows) == 1



# =========================================================================== Ask AI (v0.7)
def test_ask_ai_panel_streams_an_answer_and_saves_it_to_the_notes(
    window: MainWindow, library: Library, make_pdf: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    import threading

    from bibmanager.core import assistant as core_assistant
    from bibmanager.gui import assistant_panel as panel_module

    pid = _ingest(library, make_pdf, "Anand", year=2012, lines=["We assume incompressibility, J = 1."])
    window.reload_view()
    window.select_ids([pid])
    window.enter_study()
    panel = window.study.assistant
    assert panel.paper is not None and panel.paper.id == pid
    assert "No API key for Claude" in panel.info.text()

    seen: dict[str, object] = {}

    def fake_ask(self, context, history, question, on_text, cancelled=lambda: False):  # type: ignore[no-untyped-def]
        seen.update(context=context, history=list(history), question=question, model=self.settings.model,
                    key=self.api_key, thread=threading.current_thread().name)  # fmt: skip
        for piece in ("Incompressibility, ", "$J = 1$ (p. 1)."):
            on_text(piece)
        return core_assistant.Usage(1200, 12, 1000)

    monkeypatch.setattr(core_assistant.AssistantClient, "ask", fake_ask)
    monkeypatch.setattr(panel_module.QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    panel.keys.set("anthropic", "sk-test")
    panel.model.setCurrentText("claude-test")
    assert library.config.assistant_models["anthropic"] == "claude-test"
    panel.question.setPlainText("Which assumptions?")
    panel._send_clicked()

    def wait(predicate) -> None:  # type: ignore[no-untyped-def]
        import time

        deadline = time.monotonic() + 10
        while not predicate() and time.monotonic() < deadline:
            QApplication.processEvents()
            time.sleep(0.01)
        assert predicate()

    wait(lambda: panel._job is None and pid in panel.conversations)
    exchange = panel.conversations[pid].exchanges[-1]
    assert exchange.answer == "Incompressibility, $J = 1$ (p. 1)." and exchange.done and not exchange.error
    assert seen["question"] == "Which assumptions?" and seen["model"] == "claude-test" and seen["key"] == "sk-test"
    assert seen["thread"] != threading.current_thread().name  # PDF reading and HTTP ran in a worker thread
    context = seen["context"]
    assert "--- page 1 ---" in context.text and "incompressibility" in context.text  # type: ignore[attr-defined]
    assert "Incompressibility" in panel.transcript.toPlainText()
    assert library.config.assistant_acknowledged == ("anthropic",)
    assert "anthropic" in (window.user_config).read_text()  # settings saved, the key is not
    assert "sk-test" not in window.user_config.read_text()

    panel.question.setPlainText("And the boundary conditions?")  # a follow-up carries the history
    panel._send_clicked()
    wait(lambda: panel._job is None and len(panel.conversations[pid].exchanges) == 2)
    assert [m.role for m in seen["history"]] == ["user", "assistant"]  # type: ignore[attr-defined]

    panel.save_to_notes()
    notes = library.get_paper(pid).notes
    assert "Ask AI" in notes and "Q: And the boundary conditions?" in notes and "$J = 1$" in notes
    assert window.study.editor.toPlainText() == notes
    window.exit_study()
    assert window.inspector.notes.toPlainText() == notes

    launched: list[str] = []
    revealed: list[Path] = []
    panel._launch = lambda url: launched.append(url.toString())
    panel._reveal = revealed.append
    panel.question.setPlainText("What is J?")
    panel.ask_in_browser("Claude", "https://claude.ai/new")
    assert launched == ["https://claude.ai/new"] and revealed and revealed[0].suffix == ".pdf"
    assert "Question: What is J?" in QGuiApplication.clipboard().text()


# =========================================================================== ZIP export of a project
def test_export_project_pdfs_as_zip_from_the_project_menu(
    window: MainWindow, library: Library, make_pdf: Callable[..., Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import zipfile
    from functools import partial

    from PySide6.QtWidgets import QFileDialog

    from bibmanager.core.export import ZipOptions
    from bibmanager.gui.dialogs import ZipExportDialog

    project = library.create_collection("Soft robotics")
    chapter = library.create_collection("Chapter 2", parent_id=project.id)
    _ingest(library, make_pdf, "Anand", year=2023, collection_id=project.id)
    _ingest(library, make_pdf, "Ogden", year=1972, collection_id=chapter.id)
    window.reload_tree(select=(NodeKind.FOLDER, project.id))
    QApplication.processEvents()

    def accept(dialog: ZipExportDialog) -> Any:
        assert dialog.windowTitle() == "Export PDFs — Soft robotics" and dialog.bib.isChecked()
        assert not dialog.subprojects.isHidden() and not dialog.subprojects.isChecked()
        assert "<b>1 PDF</b> from 1 reference" in dialog.summary.text()
        dialog.subprojects.setChecked(True)  # the plan is recomputed with the sub-project
        assert "<b>2 PDFs</b> from 2 references" in dialog.summary.text()
        return ZipExportDialog.DialogCode.Accepted

    share = tmp_path / "share"
    share.mkdir()
    monkeypatch.setattr(ZipExportDialog, "exec", accept)
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a, **k: (str(share / "for_colleagues"), ""))
    reports: list[Any] = []
    monkeypatch.setattr("bibmanager.gui.main_window.show_zip_report", lambda _p, plan, rep: reports.append(rep) or True)
    revealed: list[Path] = []
    monkeypatch.setattr(window, "_reveal", revealed.append)
    menu = window.tree_menu_for(window.tree.node_at(
        window.tree.visualRect(window.tree.find_index(NodeKind.FOLDER, project.id)).center()
    ))  # fmt: skip
    labels = [action.text() for action in menu.actions()]
    menu.actions()[labels.index("Export PDFs as ZIP...")].trigger()  # right-click on the project
    window.runner.wait_idle()
    QApplication.processEvents()

    assert "Export PDFs as ZIP..." in labels and window._zip_job is None and not window.zip_exporter.running
    (report,) = reports
    target = share / "for_colleagues.zip"
    assert report.target == target and report.pdfs == 2 and report.bib and revealed == [target]
    with zipfile.ZipFile(target) as archive:
        assert sorted(archive.namelist()) == [
            "soft_robotics/Anand_2023.pdf", "soft_robotics/Ogden_1972.pdf", "soft_robotics/soft_robotics.bib"
        ]  # fmt: skip
    assert window._zip_options() == ZipOptions(include_bib=True, include_supplements=False, include_subprojects=True)
    assert window.settings.value("zipExport/lastDir") == str(share)
    assert window.act_export_zip in window.palette_actions()

    # Cancel from the progress dialog: the worker stops at the next chunk and leaves no partial archive behind.
    import threading

    from bibmanager.gui.workers import ZipExportController

    gate = threading.Event()
    report_progress = ZipExportController._report_progress

    def held(self: ZipExportController, done: int, total: int) -> None:  # worker thread waits for the click
        gate.wait(10)
        report_progress(self, done, total)

    monkeypatch.setattr(ZipExportController, "_report_progress", held)
    monkeypatch.setattr(ZipExportDialog, "exec", lambda dialog: dialog.subprojects.isChecked())  # remembered: on
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a, **k: (str(share / "cancelled.zip"), ""))
    window.export_project_zip()  # File menu / command palette: the selected project
    assert window.zip_exporter.running and window._zip_job is not None
    window._zip_job[1].canceled.emit()  # the user clicks Cancel (the button emits canceled)
    gate.set()
    window.runner.wait_idle()
    assert not window.zip_exporter.running and window._zip_job is None
    assert window.status_label.text() == "ZIP export cancelled" and len(reports) == 1
    assert sorted(p.name for p in share.iterdir()) == ["for_colleagues.zip"]

    empty = library.create_collection("Empty")  # nothing to export: the button is disabled
    dialog = ZipExportDialog(window, planner=partial(library.plan_project_zip, empty.id), options=ZipOptions())
    assert not dialog.export_button.isEnabled() and "nothing to export" in dialog.summary.text()
    dialog.deleteLater()
