"""Manuscript check (core/manuscript.py), the new query flags and the saved searches of the configuration."""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

from bibmanager.core import AppConfig, Author, Paper, PaperMetadata
from bibmanager.core.config import DEFAULT_SAVED_SEARCHES
from bibmanager.core.manuscript import citation_keys, compare, manuscript_folder, scan_citations, tex_files
from bibmanager.core.query import build_record, matches, parse_query

REPO_CONFIG = Path(__file__).resolve().parents[1] / "config.json"


def test_citation_commands_of_natbib_and_biblatex_are_recognised() -> None:
    text = r"""
    \cite{Simo_1987} \citep[see][p.~3]{Reese_Govindjee_1998, Bergstrom_Boyce_1998}
    % \cite{Commented_2000}  a comment is ignored
    50\% of the cases \textcite{Ogden_Roxburgh_1999}\parencites[cf.][]{Miehe_et_al_2010}{Ambati_et_al_2015}
    \citeauthor*{Gent_1996} \Citet{Arruda_Boyce_1993} \autocite[12]{Holzapfel_et_al_2000}
    \cite
      {Broken_Line_2001}
    """
    keys, everything = citation_keys(text)
    assert keys == {
        "Simo_1987", "Reese_Govindjee_1998", "Bergstrom_Boyce_1998", "Ogden_Roxburgh_1999", "Miehe_et_al_2010",
        "Ambati_et_al_2015", "Gent_1996", "Arruda_Boyce_1993", "Holzapfel_et_al_2000", "Broken_Line_2001",
    }  # fmt: skip
    assert not everything
    assert citation_keys(r"\nocite{*}") == (set(), True)


def test_scan_reads_tex_files_below_the_folder_and_skips_hidden_and_build_folders(tmp_path: Path) -> None:
    (tmp_path / "chapters").mkdir()
    (tmp_path / ".git").mkdir()
    (tmp_path / "build").mkdir()
    (tmp_path / "main.tex").write_text(r"\input{chapters/one} \cite{A_2020}", encoding="utf-8")
    (tmp_path / "chapters" / "one.tex").write_text(r"\citet{B_2021}", encoding="utf-8")
    (tmp_path / ".git" / "x.tex").write_text(r"\cite{Hidden_1999}", encoding="utf-8")
    (tmp_path / "build" / "y.tex").write_text(r"\cite{Built_1999}", encoding="utf-8")
    assert [p.name for p in tex_files(tmp_path)] == ["main.tex", "one.tex"]
    scan = scan_citations(tmp_path)
    assert scan.keys == {"A_2020", "B_2021"} and len(scan.tex_files) == 2
    assert scan_citations(tmp_path / "missing").tex_files == []


def test_compare_ignores_case_like_bibtex_and_reports_what_the_library_can_add(tmp_path: Path) -> None:
    (tmp_path / "paper.tex").write_text(r"\cite{simo_1987, Reese_Govindjee_1998, Unknown_2001}", encoding="utf-8")
    library = ["Simo_1987", "Gent_1996", "Reese_Govindjee_1998"]
    check = compare(scan_citations(tmp_path), ["Simo_1987", "Gent_1996"], library)
    assert check.missing == ["Reese_Govindjee_1998", "Unknown_2001"]
    assert check.missing_in_library == ["Reese_Govindjee_1998"]
    assert check.unused == ["Gent_1996"] and check.cited_in_project == 1
    (tmp_path / "paper.tex").write_text(r"\nocite{*} \cite{Simo_1987}", encoding="utf-8")
    assert compare(scan_citations(tmp_path), ["Simo_1987", "Gent_1996"], []).unused == []  # all printed anyway
    assert manuscript_folder(str(tmp_path / "refs.bib")) == tmp_path
    assert manuscript_folder(None) is None and manuscript_folder(str(tmp_path / "nope" / "refs.bib")) is None


def test_project_and_opened_flags_in_queries() -> None:
    paper = Paper(id=1, sha256=None, citation_key="Simo_1987", relative_pdf_path=None,
                  meta=PaperMetadata(title="Damage", authors=[Author("Simo", "J. C.")]))  # fmt: skip
    loose = build_record(paper, [])
    filed = build_record(paper, ["Viscoelastic fracture"])
    assert matches(parse_query("-has:project"), loose) and not matches(parse_query("-has:project"), filed)
    assert matches(parse_query("has:folder"), filed)
    assert not matches(parse_query("is:opened"), loose)
    paper.last_opened_at = "2026-11-16T10:00:00+00:00"
    assert matches(parse_query("is:opened"), build_record(paper, []))


def test_saved_searches_default_round_trip_and_survive_an_empty_list(tmp_path: Path) -> None:
    config = AppConfig.load(REPO_CONFIG, library_root=tmp_path / "lib")
    assert config.saved_searches == DEFAULT_SAVED_SEARCHES
    config = dataclasses.replace(
        config,
        saved_searches=(("Starred 2020s", "is:starred year:2020-", "year"), ("  ", "ignored", ""), ("X", "", "bad")),
    )
    assert config.saved_searches[1] == ("X", "", "")  # unknown sort keys are dropped
    config = dataclasses.replace(config, saved_searches=config.saved_searches[:1])
    assert config.saved_searches == (("Starred 2020s", "is:starred year:2020-", "year"),)
    user = tmp_path / "user.json"
    config.save_user_settings(user)
    assert json.loads(user.read_text(encoding="utf-8"))["saved_searches"] == [
        {"name": "Starred 2020s", "query": "is:starred year:2020-", "sort": "year"}
    ]
    assert AppConfig.load(user).saved_searches == (("Starred 2020s", "is:starred year:2020-", "year"),)
    config.saved_searches = ()
    config.save_user_settings(user)
    assert AppConfig.load(user).saved_searches == ()  # removing every search is remembered
