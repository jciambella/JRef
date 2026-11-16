"""Turn a Mendeley-organised library folder into JRef's layout, updating library.db as files move.

Run with JRef closed, on the library folder that holds library.db (a library that is not shared yet):

    python packaging/tools/reorganize_library_folder.py <library folder> [--budget SECONDS] [--dry-run]

It is resumable: every run continues where the previous one stopped (``--budget`` limits one run; the state is in
the database itself and in ``_legacy/reorganization/``). Phases:

1. backup   library.db -> _legacy/reorganization/library.db.before (once)
2. store    PDFs linked in place (Mendeley's ``<year>/<Authors>_<year>.pdf``) -> pdfs/<year>/<key>.pdf
            (``Library.move_into_storage``, mode "move")
3. evidence first-page text of the PDFs no entry links to, when an entry without PDF has the same authors and year
4. attach   those PDFs to their entry when the name matches and nothing on the first pages contradicts it (a DOI of
            another paper); several files and entries with the same authors and year are told apart by DOI or title
            printed on the first pages; ties are left alone
5. rename   citation keys and managed PDFs to the naming rule (``Library.apply_naming_rule``), if ``--rename-keys``
6. inbox    remaining PDFs -> inbox/<old folder>/ (JRef imports them at its next start; duplicates are set aside
            in inbox/Duplicates); other remaining files -> _legacy/unfiled/<old folder>/
7. legacy   Mendeley exports and backups at the top level -> _legacy/mendeley/; ToRead/, Archive/ -> _legacy/
8. finish   all .bib files rewritten; a report

Every move is logged in _legacy/reorganization/moves.csv (old path, new path, reason). Nothing is deleted.
"""

from __future__ import annotations

import argparse
import collections
import csv
import json
import os
import re
import shutil
import sqlite3
import sys
import time
from pathlib import Path, PurePosixPath

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from unidecode import unidecode  # noqa: E402

from bibmanager.core import AppConfig, Library  # noqa: E402
from bibmanager.core.extractor import _compact, clean_doi, find_dois, read_pdf, title_printed_in  # noqa: E402
from bibmanager.core.relink import norm  # noqa: E402
from bibmanager.core.sync import is_shared  # noqa: E402

YEAR_DIR = re.compile(r"^\d{4}$")
MENDELEY_NAME = re.compile(r"^(?P<auth>.*)_(?P<year>\d{4}|Unknown)(?:\((?P<n>\d+)\))?$")
JREF_ITEMS = {"pdfs", "bibtex", "inbox", "sync", "_legacy", "library.db", "library.db-journal", "library.db-wal",
              "library.db-shm"}  # fmt: skip
INBOX_DIRS_EXTRA = ("Unknown",)  # with the year folders: unmatched PDFs go to the inbox
LEGACY_DIRS = ("ToRead", "Archive")  # searched for PDFs of entries without one; what is left goes to _legacy/
MIN_TEXT = 200  # characters of text on the first two pages below which a PDF counts as a scan
_WORD = re.compile(r"[a-z0-9]+")


def title_mostly_printed(title: str, compact_text: str) -> bool:
    """The title is on the page up to OCR noise and lost ligatures ("fibre" read as "rbre").

    Checked on a sample of the real library: single words are not enough (another paper by the same authors shares
    most of them), pairs of consecutive words are: >= 70 % of the pairs, or >= 50 % with >= 85 % of the words.
    """
    words = _WORD.findall(unidecode(title).lower())
    pairs = [a + b for a, b in zip(words, words[1:], strict=False)]
    long_words = [w for w in words if len(w) >= 4]
    if not pairs or not long_words:
        return False
    pair_share = sum(1 for p in pairs if p in compact_text) / len(pairs)
    word_share = sum(1 for w in long_words if w in compact_text) / len(long_words)
    return pair_share >= 0.7 or (pair_share >= 0.5 and word_share >= 0.85)


class Budget:
    def __init__(self, seconds: float) -> None:
        self.end = time.monotonic() + seconds

    @property
    def over(self) -> bool:
        return time.monotonic() > self.end


class Reorganizer:
    def __init__(self, root: Path, *, budget: Budget, dry_run: bool, rename_keys: bool) -> None:
        self.root = root
        self.budget = budget
        self.dry = dry_run
        self.rename_keys = rename_keys
        self.state_dir = root / "_legacy" / "reorganization"
        self.state_file = self.state_dir / "state.json"
        self.state: dict = {}
        self.moves_file = self.state_dir / "moves.csv"

    # ------------------------------------------------------------------ helpers
    def load_state(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        if self.state_file.is_file():
            self.state = json.loads(self.state_file.read_text(encoding="utf-8"))

    def save_state(self) -> None:
        tmp = self.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state), encoding="utf-8")
        os.replace(tmp, self.state_file)

    def log_move(self, old: str, new: str, reason: str) -> None:
        new_file = not self.moves_file.exists()
        with self.moves_file.open("a", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            if new_file:
                writer.writerow(["old path", "new path", "reason"])
            writer.writerow([old, new, reason])

    def rel(self, path: Path) -> str:
        return path.relative_to(self.root).as_posix()

    def move_file(self, source: Path, target: Path, reason: str) -> Path | None:
        """Move without ever overwriting: an existing target gets " (n)" before its extension."""
        candidate, n = target, 2
        while candidate.exists():
            candidate = target.with_name(f"{target.stem} ({n}){target.suffix}")
            n += 1
        if self.dry:
            print(f"would move {self.rel(source)} -> {self.rel(candidate)}")
            return candidate
        try:
            candidate.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(source), str(candidate))
        except OSError as exc:  # e.g. a file held open by the sync client: reported, the next run tries again
            self.state.setdefault("move_failed", {})[self.rel(source)] = str(exc)
            return None
        self.log_move(self.rel(source), self.rel(candidate), reason)
        return candidate

    def open_library(self) -> Library:
        if is_shared(self.root):
            raise SystemExit("This library is shared between devices: reorganise it before sharing it.")
        config = AppConfig.load(
            None, library_root=self.root, import_mode="move", inbox_enabled=False, sync_enabled=False
        )
        return Library(config)

    def loose_files(self, library: Library, *, include_legacy: bool = True) -> list[Path]:
        """Files in the Mendeley folders that no entry links to (main PDF or supplementary file)."""
        extra = (*INBOX_DIRS_EXTRA, *LEGACY_DIRS) if include_legacy else INBOX_DIRS_EXTRA
        recorded = {p.relative_pdf_path for p in library.papers() if p.relative_pdf_path}
        recorded |= {r[0] for r in library.db.conn.execute("SELECT relative_path FROM attachments")}
        found = []
        for entry in sorted(os.listdir(self.root)):
            folder = self.root / entry
            if not folder.is_dir() or not (YEAR_DIR.match(entry) or entry in extra):
                continue
            for dirpath, _dirs, names in os.walk(folder):
                for name in sorted(names):
                    path = Path(dirpath) / name
                    if self.rel(path) not in recorded:
                        found.append(path)
        return found

    # ------------------------------------------------------------------ phases
    def backup(self) -> None:
        target = self.state_dir / "library.db.before"
        if not target.exists() and not self.dry:
            source = sqlite3.connect(str(self.root / "library.db"))
            copy = sqlite3.connect(str(target))
            with copy:
                source.backup(copy)
            copy.close()
            source.close()
            print(f"Backup: {self.rel(target)}")

    def store(self, library: Library) -> bool:
        rows = library.db.conn.execute(
            "SELECT id, relative_pdf_path FROM papers WHERE pdf_linked = 1 AND relative_pdf_path IS NOT NULL"
        ).fetchall()
        todo = [(pid, rel) for pid, rel in rows if not os.path.isabs(rel) and (self.root / rel).is_file()]
        done = 0
        library.begin_bulk()
        try:
            for pid, rel in todo:
                if self.budget.over:
                    break
                if self.dry:
                    done += 1
                    continue
                try:
                    paper = library.move_into_storage(pid, mode="move")
                except Exception as exc:  # reported, the PDF stays linked where it is
                    self.state.setdefault("store_failed", {})[rel] = str(exc)
                    done += 1
                    continue
                self.log_move(rel, paper.relative_pdf_path or "", f"stored ({paper.citation_key})")
                done += 1
                if done % 200 == 0:
                    print(f"  {done} of {len(todo)}", flush=True)
        finally:
            library.end_bulk()
        failed = len(self.state.get("store_failed", {}))
        print(f"store: {done} of {len(todo)} linked PDFs handled ({failed} could not be moved, still linked)")
        return done == len(todo)

    def _expected_names(self, library: Library) -> dict[tuple[str, str], list]:
        groups: dict[tuple[str, str], list] = collections.defaultdict(list)
        for paper in library.papers():
            if paper.has_pdf:
                continue
            names = [a.family or a.literal for a in paper.meta.authors]
            if names:
                authors = ", ".join(names) if len(names) <= 3 else f"{names[0]} et al."
                groups[(norm(authors), paper.meta.year_label)].append(paper)
        return groups

    def evidence(self, library: Library) -> bool:
        """DOIs and text of the first two pages of every PDF that no entry links to (cached, resumable)."""
        cache_file = self.state_dir / "evidence.json"
        cache = json.loads(cache_file.read_text(encoding="utf-8")) if cache_file.is_file() else {}
        todo = [p for p in self.loose_files(library) if p.suffix.lower() == ".pdf" and self.rel(p) not in cache]
        done = 0
        for path in todo:
            if self.budget.over:
                break
            try:
                text = read_pdf(path, max_pages=2).full_text
            except Exception:
                text = ""
            # in printed order: the first DOI is usually the paper's own
            dois = list(dict.fromkeys(clean_doi(d) for d in find_dois(text)))
            cache[self.rel(path)] = [dois, _compact(text)[:20000]]
            done += 1
            if done % 100 == 0:
                cache_file.write_text(json.dumps(cache), encoding="utf-8")
                print(f"  {done} of {len(todo)}", flush=True)
        cache_file.write_text(json.dumps(cache), encoding="utf-8")
        print(f"evidence: {done} of {len(todo)} PDFs read ({len(cache)} in total)")
        return done == len(todo)

    def plan_matches(self, library: Library) -> list[tuple[str, int, str]]:
        """(file, entry, reason) pairs: by DOI printed on the first pages, then by Mendeley's file name."""
        cache = json.loads((self.state_dir / "evidence.json").read_text(encoding="utf-8"))
        groups = self._expected_names(library)
        without_pdf = {p.id: p for papers in groups.values() for p in papers}
        for paper in library.papers():  # entries without authors are matched by DOI only
            if not paper.has_pdf:
                without_pdf.setdefault(paper.id, paper)
        loose = [self.rel(p) for p in self.loose_files(library) if p.suffix.lower() == ".pdf" and self.rel(p) in cache]

        def name_key(rel: str) -> tuple[str, str] | None:
            match = MENDELEY_NAME.match(PurePosixPath(rel).stem)
            return (norm(match["auth"]), match["year"]) if match else None

        def score(rel: str, paper) -> int:
            """3: its DOI printed first, 2: its DOI printed (after another: maybe a citation), 1: its title printed,
            0: nothing known (no text: a scan) or most title words printed (OCR noise), -1: readable text that does not
            print the title (an orphaned file of another paper by the same authors), -2: other DOIs only.
            Ties between candidates are never resolved by guessing."""
            dois, compact = cache[rel][0], cache[rel][1]
            doi = clean_doi(paper.meta.doi) if paper.meta.doi else ""
            if doi and dois:
                return 3 if dois[0] == doi else 2 if doi in dois else -2
            if paper.meta.title and compact and title_printed_in(paper.meta.title, compact):
                return 1
            if len(compact) < MIN_TEXT or not paper.meta.title:
                return 0
            return 0 if title_mostly_printed(paper.meta.title, compact) else -1

        plan: list[tuple[str, int, str]] = []
        used_r: set[str] = set()
        used_p: set[int] = set()

        # 1. DOI: the entry's DOI is printed in exactly one file, which prints no other entry's DOI; a DOI on the
        #    first pages can be a citation (part I of a paper cites part II), so the title or the name must agree too
        by_doi: dict[str, list[int]] = collections.defaultdict(list)
        for paper in without_pdf.values():
            if paper.meta.doi:
                by_doi[clean_doi(paper.meta.doi)].append(paper.id)
        files_of: dict[int, list[str]] = collections.defaultdict(list)
        for rel in loose:
            for doi in cache[rel][0][:1]:  # the first DOI printed: the paper's own, not one it cites
                for pid in by_doi.get(doi, []):
                    files_of[pid].append(rel)
        entries_of: dict[str, list[int]] = collections.defaultdict(list)
        for pid, rels in files_of.items():
            for rel in rels:
                entries_of[rel].append(pid)
        for pid, rels in sorted(files_of.items()):
            paper = without_pdf[pid]
            if len(rels) != 1 or len(entries_of[rels[0]]) != 1 or len(by_doi[clean_doi(paper.meta.doi)]) != 1:
                continue
            rel = rels[0]
            names = [a.family or a.literal for a in paper.meta.authors]
            same_name = name_key(rel) is not None and name_key(rel) in {
                k for k, papers in groups.items() if any(p.id == pid for p in papers)
            }
            printed = bool(paper.meta.title) and title_printed_in(paper.meta.title, cache[rel][1])
            if same_name or printed or (len(cache[rel][0]) == 1 and not names):
                plan.append((rel, pid, "DOI + " + ("name" if same_name else "title" if printed else "only DOI")))
                used_r.add(rel)
                used_p.add(pid)

        # 2. Mendeley's file name "<Authors>_<year>[(n)].pdf", told apart by content when there are several
        files: dict[tuple[str, str], list[str]] = collections.defaultdict(list)
        for rel in loose:
            key = name_key(rel)
            if key is not None and rel not in used_r:
                files[key].append(rel)
        self.ambiguous: list[tuple[list[str], list[str]]] = []
        for key, rels in files.items():
            papers = [p for p in groups.get(key, []) if p.id not in used_p]
            if not papers:
                continue
            if len(rels) == 1 and len(papers) == 1:
                s = score(rels[0], papers[0])
                if s >= 0:
                    plan.append((rels[0], papers[0].id, "name" + {3: " + DOI", 2: " + DOI", 1: " + title", 0: ""}[s]))
                else:
                    self.ambiguous.append((rels, [papers[0].citation_key]))
                continue
            scores = {(r, p.id): score(r, p) for r in rels for p in papers}
            g_r: set[str] = set()
            g_p: set[int] = set()
            for (r, pid), s in sorted(scores.items(), key=lambda item: -item[1]):
                if s <= 0 or r in g_r or pid in g_p:
                    continue
                rivals = [s2 for (r2, p2), s2 in scores.items()
                          if (r2 == r) != (p2 == pid) and s2 >= s and r2 not in g_r and p2 not in g_p]  # fmt: skip
                if rivals:
                    continue
                plan.append((r, pid, "name + content (" + ("DOI" if s >= 2 else "title") + ")"))
                g_r.add(r)
                g_p.add(pid)
            left_r = [r for r in rels if r not in g_r]
            left_p = [p for p in papers if p.id not in g_p]
            if len(left_r) == 1 and len(left_p) == 1 and scores[(left_r[0], left_p[0].id)] >= 0 and (g_r or g_p):
                plan.append((left_r[0], left_p[0].id, "name (the only one left)"))
            elif left_r and left_p:
                self.ambiguous.append((left_r, [p.citation_key for p in left_p]))
        return plan

    def attach(self, library: Library) -> bool:
        plan = self.plan_matches(library)
        done = failed = 0
        library.begin_bulk()
        try:
            for rel, pid, reason in plan:
                if self.budget.over:
                    break
                path = self.root / rel
                if not path.is_file():
                    continue
                if self.dry:
                    done += 1
                    continue
                try:
                    candidate = library.prepare_attachment(path)
                    paper = library.attach_pdf(pid, candidate, mode="move")
                except Exception as exc:  # e.g. the same PDF is already in the library: it goes to the inbox
                    failed += 1
                    self.state.setdefault("attach_failed", {})[rel] = str(exc)
                    continue
                self.log_move(rel, paper.relative_pdf_path or "", f"attached to {paper.citation_key}: {reason}")
                done += 1
                if done % 100 == 0:
                    print(f"  {done} of {len(plan)}", flush=True)
        finally:
            library.end_bulk()
        remaining = sum(1 for rel, _pid, _r in plan if (self.root / rel).is_file()) - failed
        self.state["ambiguous"] = self.ambiguous
        print(f"attach: {done} PDFs attached, {failed} refused, {len(self.ambiguous)} ambiguous groups left alone")
        return remaining <= 0 or self.dry

    def rename(self, library: Library) -> bool:
        if not self.rename_keys:
            return True
        outdated = library.papers_with_outdated_keys()
        done = 0
        chunk = 100
        for start in range(0, len(outdated), chunk):
            if self.budget.over:
                break
            ids = [p.id for p, _base in outdated[start : start + chunk]]
            if self.dry:
                done += len(ids)
                continue
            for old, new in library.apply_naming_rule(ids):
                self.state.setdefault("renamed", []).append([old, new])
            done += len(ids)
            self.save_state()
        print(f"rename: {done} of {len(outdated)} keys renamed to the naming rule")
        return done >= len(outdated)

    def candidates(self, library: Library) -> dict[str, list[str]]:
        """Loose PDFs that may belong to an entry still without PDF (same Mendeley name, or its DOI printed first),
        with those entries' keys: not attached because the evidence was not conclusive."""
        cache_file = self.state_dir / "evidence.json"
        cache = json.loads(cache_file.read_text(encoding="utf-8")) if cache_file.is_file() else {}
        groups = self._expected_names(library)
        by_doi: dict[str, list[str]] = collections.defaultdict(list)
        for paper in library.papers():
            if not paper.has_pdf and paper.meta.doi:
                by_doi[clean_doi(paper.meta.doi)].append(paper.citation_key)
        found: dict[str, list[str]] = {}
        for path in self.loose_files(library):
            if path.suffix.lower() != ".pdf":
                continue
            rel = self.rel(path)
            keys: list[str] = []
            match = MENDELEY_NAME.match(path.stem)
            if match:
                keys += [p.citation_key for p in groups.get((norm(match["auth"]), match["year"]), [])]
            for doi in cache.get(rel, [[]])[0][:1]:
                keys += [k for k in by_doi.get(doi, []) if k not in keys]
            if keys:
                found[rel] = keys
        return found

    def to_inbox(self, library: Library) -> bool:
        inbox = self.root / "inbox"
        to_check = self.root / "_legacy" / "to_check"
        candidates = self.candidates(library)
        if candidates and not self.dry:
            with (self.state_dir / "to_check.csv").open("w", newline="", encoding="utf-8") as fh:
                writer = csv.writer(fh)
                writer.writerow(["file (now in _legacy/to_check/)", "entries without PDF it may belong to"])
                writer.writerows([rel, " ".join(keys)] for rel, keys in sorted(candidates.items()))
        moved_pdf = moved_other = checked = 0
        for path in self.loose_files(library):
            if self.budget.over:
                return False
            rel = self.rel(path)
            folder = rel.split("/")[0]
            if rel in candidates:  # the inbox would import it as a second entry: attach it by hand instead
                self.move_file(path, to_check / rel, "may belong to " + " ".join(candidates[rel]))
                checked += 1
            elif folder in LEGACY_DIRS:
                continue  # moved with its folder by the next phase
            elif path.suffix.lower() == ".pdf":
                self.move_file(path, inbox / folder / path.name, "no entry: to the inbox")
                moved_pdf += 1
            else:
                self.move_file(path, self.root / "_legacy" / "unfiled" / self.rel(path), "not a PDF, no entry")
                moved_other += 1
        print(
            f"inbox: {moved_pdf} PDFs moved to inbox/, {checked} to _legacy/to_check/ (see to_check.csv), "
            f"{moved_other} other files to _legacy/unfiled/"
        )
        return True

    def legacy(self) -> bool:
        moved = 0
        for entry in sorted(os.listdir(self.root)):
            path = self.root / entry
            if entry in JREF_ITEMS or entry.startswith("."):
                continue
            if path.is_file() and (entry.endswith(".bib") or entry.endswith(".zip") or entry.startswith("library.db.")):
                self.move_file(path, self.root / "_legacy" / "mendeley" / entry, "Mendeley export or backup")
                moved += 1
            elif path.is_dir() and entry in LEGACY_DIRS:
                for dirpath, _dirs, names in os.walk(path):
                    for name in names:
                        source = Path(dirpath) / name
                        self.move_file(source, self.root / "_legacy" / self.rel(source), "kept, not a library file")
                        moved += 1
        print(f"legacy: {moved} files moved to _legacy/")
        return True

    def empty_folders(self) -> list[str]:
        empty = []
        for entry in sorted(os.listdir(self.root)):
            path = self.root / entry
            if path.is_dir() and (YEAR_DIR.match(entry) or entry in (*INBOX_DIRS_EXTRA, *LEGACY_DIRS)):
                if not any(p.is_file() for p in path.rglob("*")):
                    empty.append(entry)
        return empty

    def finish(self, library: Library) -> None:
        if not self.dry:
            library.sync_all_bibs()
        papers = library.papers()
        managed = [p for p in papers if p.pdf_managed]
        missing = [p.citation_key for p in managed if not library.pdf_available(p)]
        linked = [p for p in papers if p.pdf_linked]
        self.state["report"] = {
            "entries": len(papers),
            "with_pdf_in_pdfs": len(managed),
            "managed_missing_on_disk": missing,
            "still_linked": len(linked),
            "without_pdf": sum(1 for p in papers if not p.has_pdf),
            "inbox_pdfs": sum(1 for p in (self.root / "inbox").rglob("*.pdf")),
            "empty_old_folders": self.empty_folders(),
        }
        before = self.state_dir / "library.db.before"
        if before.is_file():  # old key -> new key, for the \\cite{} commands of existing manuscripts
            old = sqlite3.connect(f"file:{before.as_posix()}?mode=ro", uri=True)
            old_keys = dict(old.execute("SELECT id, citation_key FROM papers"))
            old.close()
            changed = sorted((old_keys[p.id], p.citation_key) for p in papers
                             if p.id in old_keys and old_keys[p.id] != p.citation_key)  # fmt: skip
            with (self.state_dir / "renamed_keys.csv").open("w", newline="", encoding="utf-8") as fh:
                writer = csv.writer(fh)
                writer.writerow(["old key", "new key"])
                writer.writerows(changed)
            self.state["report"]["keys_changed"] = len(changed)
        for name in ("store_failed", "attach_failed", "move_failed"):
            if self.state.get(name):
                self.state["report"][name] = len(self.state[name])
        self.state["report"]["ambiguous_groups"] = len(self.state.get("ambiguous", []))
        report = dict(self.state["report"])
        report["empty_old_folders"] = len(report["empty_old_folders"])
        print(json.dumps(report, indent=1, ensure_ascii=False)[:3000])

    # ------------------------------------------------------------------ driver
    PHASES = ("backup", "store", "evidence", "attach", "rename", "inbox", "legacy", "finish")

    def run(self) -> bool:
        self.load_state()
        done = set(self.state.get("done", []))
        library = self.open_library()
        try:
            for phase in self.PHASES:
                if phase in done:
                    continue
                if self.budget.over:
                    break
                if self.dry and phase in ("inbox", "legacy", "finish"):
                    continue  # they depend on the moves a dry run does not make
                print(f"--- {phase}")
                if phase == "backup":
                    self.backup()
                    finished = True
                elif phase == "store":
                    finished = self.store(library)
                elif phase == "evidence":
                    finished = self.evidence(library)
                elif phase == "attach":
                    finished = self.attach(library)
                elif phase == "rename":
                    finished = self.rename(library)
                elif phase == "inbox":
                    finished = self.to_inbox(library)
                elif phase == "legacy":
                    finished = self.legacy()
                else:
                    self.finish(library)
                    finished = True
                if finished and not self.dry:
                    done.add(phase)
                    self.state["done"] = sorted(done, key=self.PHASES.index)
                self.save_state()
                if not finished:
                    break
        finally:
            library.close()
        complete = len(done) == len(self.PHASES)
        if self.dry:
            print("Dry run: nothing was moved or changed (only the PDF text cache was written).")
        else:
            print("COMPLETE" if complete else "not finished: run again to continue")
        return complete


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("root", type=Path)
    parser.add_argument("--budget", type=float, default=1e9, help="stop after about this many seconds")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--rename-keys", action="store_true", help="apply the naming rule to keys and PDFs")
    args = parser.parse_args()
    for stream in (sys.stdout, sys.stderr):  # a Windows console may not encode every file name
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    if not (args.root / "library.db").is_file():
        print(f"No library.db in {args.root}")
        return 2
    Reorganizer(args.root, budget=Budget(args.budget), dry_run=args.dry_run, rename_keys=args.rename_keys).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
