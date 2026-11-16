<img src="packaging/branding/jref_logo_ribbon.svg" width="96" alt="JRef logo">

# JRef — Journal Reference Manager

JRef is a local, file-first reference manager for people who write papers in LaTeX. Every PDF is stored once,
named after its citation key, and every project keeps its own BibTeX file up to date — if you like, directly
inside the folder of the manuscript you are writing.

## What it does

- **Import by drag and drop.** JRef hashes the PDF (SHA-256, so duplicates are refused), reads the DOI or arXiv
  ID from the first pages, fetches the record from Crossref or arXiv, and falls back to an OpenAlex title search
  when the PDF prints no identifier.
- **Deterministic keys and file names.** `Surname_Year`, `Surname1_Surname2_Year`, `Surname1_et_al_Year`, with
  `a`, `b`, … on collisions. The same string is the BibTeX key and the file name: `pdfs/<year>/<key>.pdf`.
- **Projects are logical collections.** One PDF can belong to any number of projects without being copied.
  Each project writes `bibtex/<project>.bib` (and optionally a second copy wherever your `.tex` lives);
  acronyms and proper names in titles are protected with braces (`{Mullins}`, `{FEM}`).
- **Search** over metadata and the full text of the PDFs (SQLite FTS5), with field syntax such as
  `author:anand journal:solids year:2015-2020 is:unread`.
- Supplementary files per entry, notes, an inbox folder that imports whatever you save into it, and a
  Mendeley import that leaves your PDFs where they are.
- **Several devices** (PCs and an Android app) share one library through any synchronised folder — Dropbox,
  Google Drive, OneDrive, Qsync, Syncthing — using per-device change journals instead of a shared database.
- **Ask AI** about a paper with your own API key (OpenAI, Anthropic, Google) or a local model (Ollama).

## Quick start

**Windows:** run `JRef-<version>-Setup.exe`. It installs for the current user (no administrator rights) and
brings its own Python. Installers are not kept in this repository; see [docs/setup.md](docs/setup.md) to build one.

**From source** (Windows, macOS, Linux; Python 3.11 or later):

```bash
git clone <this repository> jref
cd jref
python -m venv .venv
.venv\Scripts\activate          # Windows;  macOS/Linux: source .venv/bin/activate
pip install -e ".[gui]"
python -m bibmanager            # or simply: jref
```

On first start JRef asks for the library folder. Read [docs/setup.md](docs/setup.md) before putting it in a
cloud-synchronised folder.

## Documentation

| Document | Contents |
|---|---|
| [docs/setup.md](docs/setup.md) | Installation, the library folder, keeping it in Dropbox / Google Drive / OneDrive, a second computer |
| [docs/ai-assistant.md](docs/ai-assistant.md) | Ask AI: OpenAI API key step by step, other providers, costs and privacy |
| [ANDROID.md](ANDROID.md) | How sharing between devices works, the phone app, building the APK |
| [SPEC.md](SPEC.md) | Functional specification, database schema, metadata pipeline |

## Repository layout

```
bibmanager/core/     pure Python: database, storage, extractor, bibtex_manager, sync, assistant, ...
bibmanager/gui/      desktop application (PySide6)
bibmanager/mobile/   Android application (Kivy)
tests/               pytest suite (core, GUI, import, sync, assistant)
packaging/           Windows installer, Android build, branding, maintenance tools
.github/workflows/   android.yml builds the APK on every push to main
```

The Python package keeps its historical name `bibmanager`; the product is JRef.

## Development

```bash
pip install -e ".[gui,dev]"
pytest
```

The core tests run without Qt; the GUI tests need PySide6 (`QT_QPA_PLATFORM=offscreen` on a headless machine).

## Licence

Private repository; all rights reserved for now (`license = "Proprietary"` in `pyproject.toml`). Note that JRef
depends on PyMuPDF (AGPL-3.0) and Unidecode (GPL): personal use is unaffected, but distributing JRef builds —
for instance the Windows installer — to other people brings those licences' terms into play. Decide the
licence before making the repository public.
