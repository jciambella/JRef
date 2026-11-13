"""Search syntax: field qualifiers, scopes, phrases, years, flags (pure core, no Qt)."""

from __future__ import annotations

import pytest

from bibmanager.core.models import Author, Paper, PaperMetadata
from bibmanager.core.query import Term, build_record, matches, parse_query


def make_paper(pid: int = 1, **kw: object) -> Paper:
    meta = PaperMetadata(
        title=str(kw.get("title", "Phase-field fracture of hydrogels")),
        authors=list(kw.get("authors", [Author("Müller", "Jörg"), Author("van der Giessen", "Erik")])),  # type: ignore[call-overload]
        year=kw.get("year", 2020),  # type: ignore[arg-type]
        journal=str(kw.get("journal", "Journal of the Mechanics and Physics of Solids")),
        doi=str(kw.get("doi", "10.1016/j.jmps.2020.104000")),
        keywords=str(kw.get("keywords", "fracture; hydrogels")),
        entry_type=str(kw.get("entry_type", "article")),
        extra_fields=dict(kw.get("extra", {})),  # type: ignore[call-overload]
    )
    return Paper(
        id=pid,
        sha256=None,
        citation_key=str(kw.get("key", "Muller_et_al_2020")),
        relative_pdf_path=kw.get("pdf"),  # type: ignore[arg-type]
        meta=meta,
        tags=str(kw.get("tags", "to read; review")),
        notes=str(kw.get("notes", "")),
        favorite=bool(kw.get("favorite", False)),
        is_read=bool(kw.get("is_read", False)),
    )


def test_parse_qualifiers_phrases_negation_and_years() -> None:
    q = parse_query('author:"van der" au:müller -tag:review journal:solids year:2015-2020 "phase field" http://x.org')
    assert q.terms == [
        Term("author", "van der"),
        Term("author", "muller"),
        Term("tag", "review", negate=True),
        Term("journal", "solids"),
        Term("year", "", year_min=2015, year_max=2020),
        Term(None, "phase field"),
        Term(None, "http://x.org"),  # unknown prefix: plain text, not swallowed
    ]
    assert q.free_text == '"phase field" http://x.org'
    years = [parse_query(f"year:{v}").terms[0] for v in ("2020", ">2018", ">=2018", "<2000", "2018-", "-1999")]
    assert [(t.year_min, t.year_max) for t in years] == [
        (2020, 2020), (2019, None), (2018, None), (None, 1999), (2018, None), (None, 1999)
    ]  # fmt: skip
    assert parse_query("is:fav has:pdf is:nonsense").terms == [
        Term("is", "fav"), Term("has", "pdf"), Term(None, "is:nonsense")
    ]  # fmt: skip
    assert not parse_query("   ")


@pytest.mark.parametrize(
    ("text", "scope", "expected"),
    [
        ("author:muller", "all", True),
        ("author:jörg", "all", True),  # accents and case ignored
        ('author:"erik van der giessen"', "all", True),  # given-family order also searchable
        ('author:"van der giessen, erik"', "all", True),
        ("author:fracture", "all", False),
        ("journal:solids", "all", True),
        ("in:mechanics year:2020", "all", True),
        ("year:2021", "all", False),
        ("year:2015-2020 title:hydrogels", "all", True),
        ("tag:review", "all", True),
        ("-tag:review", "all", False),
        ("kw:fracture", "all", True),
        ("doi:10.1016/j.jmps", "all", True),
        ("key:muller_et_al", "all", True),
        ("folder:thesis", "all", True),
        ("folder:draft", "all", False),
        ("type:article", "all", True),
        ("is:unread", "all", True),
        ("is:favorite", "all", False),
        ("has:pdf", "all", False),
        ("muller", "all", True),
        ("muller", "title", False),  # plain words follow the selected scope
        ("hydrogels", "title", True),
        ("muller -review", "all", False),  # "review" is in the tags
        ("phase field", "all", True),  # two words, each anywhere
        ('"field phase"', "all", False),
    ],
)
def test_matching(text: str, scope: str, expected: bool) -> None:
    record = build_record(make_paper(), folders=["PhD Thesis", "Hydrogels"])
    assert matches(parse_query(text), record, scope) is expected


def test_journal_qualifier_also_searches_booktitle_and_flags() -> None:
    proc = make_paper(journal="", entry_type="inproceedings", extra={"booktitle": "Proc. ECCOMAS"}, pdf="x.pdf")
    record = build_record(proc)
    assert matches(parse_query("journal:eccomas has:pdf"), record)
    assert not matches(parse_query("journal:eccomas -has:pdf"), record)
    undated = build_record(make_paper(year=None))
    assert not matches(parse_query("year:2020"), undated) and matches(parse_query("-year:2020"), undated)


def test_full_text_mode_skips_free_words_but_keeps_qualifiers() -> None:
    record = build_record(make_paper())
    q = parse_query("crack tip author:muller")
    assert not matches(q, record)  # "crack" is not in the metadata ...
    assert matches(q, record, skip_free_text=True)  # ... but the full-text index answered the words
    assert not matches(parse_query("crack author:smith"), record, skip_free_text=True)
