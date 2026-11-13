# Project Specification: Local Academic Reference Manager (Mendeley Desktop Clone)

## 1. Core Architecture & Storage Model
Build a fast, local academic reference manager in Python (`PySide6` GUI + `SQLite` with `FTS5`).
The architecture strictly decouples **Physical PDF Storage** from **Logical Project Collections**:

1. **Physical Storage (`<library_root>/pdfs/<YYYY>/`):**
   - Every PDF is stored **once** on the hard drive inside a subfolder corresponding to its publication year `<YYYY>` (or `<library_root>/pdfs/Unknown/` if the year cannot be determined yet).
   - If the publication year of a paper is edited in the UI, the physical file is automatically moved to the corresponding `<YYYY>` folder.

2. **Deterministic Author-Year Naming:**
   - **1 Author:** `surname_year.pdf` (e.g., `anand_2023.pdf`)
   - **2 Authors:** `surname1_surname2_year.pdf` (e.g., `ciambella_lancioni_2025.pdf`)
   - **3 or more Authors:** `surname1_et_al_year.pdf` (e.g., `ciambella_et_al_2025.pdf`)
   - **Normalization:** All lowercase ASCII (`unidecode`), spaces/hyphens in compound surnames converted to underscores or stripped cleanly.
   - **Collision Handling:** If a file with the same name already exists in `<YYYY>/`, append `a`, `b`, `c` after the year (e.g., `ciambella_et_al_2025a.pdf`, `ciambella_et_al_2025b.pdf`). Use the exact same identifier (without `.pdf`) as the BibTeX **Citation Key**.

3. **Logical Project Folders (Many-to-Many) & BibTeX Sync:**
   - Project folders (Collections) are virtual/logical entities stored in SQLite (`collections` and `paper_collections` M:N join table).
   - A single physical PDF can belong to multiple project collections simultaneously without duplicating the file on disk.
   - Every project collection automatically maintains a dedicated BibTeX file in `<library_root>/bibtex/<collection_slug>.bib` (plus an optional custom export path so a collection can sync directly to a specific LaTeX manuscript directory on disk).
   - A global `<library_root>/bibtex/master.bib` containing all papers is also updated automatically.

## 2. Tech Stack & Libraries
- **Language:** Python 3.11+
- **GUI:** `PySide6` (Qt6)
- **Database:** `sqlite3` with **FTS5** virtual table for full-text search across PDFs.
- **PDF Parsing:** `PyMuPDF` (`fitz`) for fast text extraction, XMP metadata reading, and first-page layout analysis.
- **Metadata APIs:** `httpx` querying:
  1. **Crossref REST API** (`https://api.crossref.org/works/<doi>`)
  2. **arXiv API** (`http://export.arxiv.org/api/query?id_list=<arxiv_id>`)
  3. **OpenAlex API** (`https://api.openalex.org/works?search=<query>`) for title/author fallback.
- **BibTeX & Text:** `bibtexparser` (v2+), `unidecode`.
- **Filesystem Monitoring:** `watchdog` (monitors an optional `<library_root>/inbox/` folder and `<library_root>/pdfs/`).

## 3. Database Schema (`library.db`)
- `papers`: `id`, `sha256` (UNIQUE), `doi`, `arxiv_id`, `citation_key` (UNIQUE), `title`, `authors_json`, `year`, `journal`, `volume`, `issue`, `pages`, `publisher`, `relative_pdf_path`, `needs_review` (BOOL), `notes`, `created_at`.
- `collections`: `id`, `name`, `parent_id` (nullable for nested project folders), `bib_export_path` (defaults to `<library_root>/bibtex/<slug>.bib`, user-overridable).
- `paper_collections`: `paper_id`, `collection_id` (PRIMARY KEY (`paper_id`, `collection_id`)).
- `papers_fts`: FTS5 virtual table indexing `paper_id`, `title`, `authors_text`, `abstract`, `full_text`.

## 4. Core Workflows

### 4.1 Drag & Drop PDF Ingestion
When one or more PDFs are dragged and dropped onto a Project Folder in the left-hand GUI tree (or onto "All Papers"):
1. **Hash Deduplication:** Compute SHA-256.
   - If the PDF already exists in `papers`, **do not** re-import or duplicate it on disk: simply link its `paper_id` to the target `collection_id` in `paper_collections` and regenerate that collection's `.bib` file.
2. **Metadata Extraction Pipeline (for new PDFs):**
   - Scan PDF XMP metadata and first 2 pages via `PyMuPDF` for DOI (`10\.\d{4,9}/[-._;()/:A-Za-z0-9]+`) or arXiv ID.
   - **If DOI found:** Fetch metadata from Crossref API.
   - **If arXiv ID found:** Fetch from arXiv API (if a published DOI is linked, fetch from Crossref).
   - **If neither found:** Extract largest-font text blocks from page 1 as candidate title/authors and query OpenAlex API.
   - **If all fail:** Mark `needs_review = True`, set `year = "Unknown"`, and keep original filename until the user enters a DOI or edits metadata in the right pane.
3. **Physical Filing & Renaming:**
   - Compute deterministic name (`surname_year.pdf` / `surname1_surname2_year.pdf` / `surname1_et_al_year.pdf`).
   - Create `<library_root>/pdfs/<year>/` if not exists and move the PDF there.
4. **Indexing & BibTeX Generation:**
   - Extract full text with `PyMuPDF` into `papers_fts`.
   - Link the paper to the target Project Collection (if dropped on one) and write the updated `.bib` file(s).

### 4.2 BibTeX Generation Rules
- Every update to a paper or collection membership immediately rewrites `<collection_slug>.bib` and `master.bib`.
- **Citation Key:** Matches the filename stem (`surname_year`, `surname1_surname2_year`, or `surname1_et_al_year`, plus collision suffix `a/b/c`).
- **LaTeX Protection:** Wrap words with >=2 uppercase letters, Roman numerals, or words listed in `config.json -> protected_words` (e.g., `{Cauchy}`, `{Piola}`, `{Kirchhoff}`, `{FEniCSx}`, `{PINNs}`, `{3D}`) in curly braces `{...}` inside the `title` field.
- Include clean fields (`author`, `title`, `journal`, `year`, `volume`, `number`, `pages`, `doi`) and omit `abstract` by default.

### 4.3 Desktop GUI Layout (3-Pane `PySide6`)
- **Left Pane (`QTreeView` - Logical Collections):**
  - Special top nodes: `All Papers`, `Needs Review`, `Unassigned`.
  - User-created Project Folders (supports creating, renaming, deleting collections, and setting a custom external `.bib` output path per project).
  - Accepts Drag & Drop of:
    1. External `.pdf` files from the OS file manager (ingests PDF into `pdfs/<YYYY>/` and assigns to that project folder).
    2. Existing rows dragged from the Center Pane table (assigns existing paper to that project folder without moving/copying the physical PDF).
- **Center Pane (`QTableView` + Search Bar):**
  - Displays papers in the currently selected Project Folder.
  - Search bar with instant toggle: `Metadata Filter` vs `Full-Text Search (FTS5)`.
  - Columns: `Citation Key`, `Authors`, `Year`, `Title`, `Journal`, `Collections` (badges/tags showing which projects it belongs to).
  - **Double-click:** Opens `<library_root>/pdfs/<YYYY>/<filename>.pdf` in the system PDF viewer.
  - **Context Menu / Shortcuts:**
    - `Ctrl+Shift+C`: Copy `\cite{citation_key}` to clipboard.
    - `Remove from this Project`: Unlinks from current collection (updates `.bib`, keeps physical PDF).
    - `Delete from Library`: Removes from all collections, updates `.bib` files, and deletes physical PDF from `pdfs/<YYYY>/` (with confirmation).
- **Right Pane (Inspector & Manual Override):**
  - Editable metadata fields (`Title`, `Authors`, `Year`, `Journal`, `Volume`, `Pages`, `DOI`, `Citation Key`).
  - Editing `Authors` or `Year` automatically renames the physical PDF and moves it to the new `<YYYY>/` folder if the year changed, updating `relative_pdf_path` and all affected `.bib` files.
  - `"Fetch by DOI"` button: pastes/queries a DOI on Crossref and updates everything automatically.

## 5. Implementation Milestones
1. **Milestone 1 (Storage, SQLite M:N & Renamer):** Implement `database.py`, `extractor.py`, and `storage.py` (enforcing `<pdfs>/<YYYY>/` hierarchy and `surname_year` / `surname1_surname2_year` / `surname1_et_al_year` collision-safe naming) + `bibtex_manager.py`. Verify with CLI tests.
2. **Milestone 2 (PySide6 3-Pane GUI & Logical Collections):** Build the UI showing logical collections on the left, paper table in the center, and live inspector on the right.
3. **Milestone 3 (Drag & Drop & Async Workers):** Add external PDF drop onto project folders (background `QThreadPool` worker for API lookup) and internal table-to-folder drag & drop for multi-project assignment.
4. **Milestone 4 (Custom `.bib` Symlink/Target & Inbox Watcher):** Allow binding any Project Folder to an external `.bib` file path (e.g., directly inside a LaTeX paper repo) so adding a paper to that project updates the manuscript's `.bib` in real time.
