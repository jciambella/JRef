# Setting up JRef

This guide covers installation, the library folder, and keeping the library in a cloud-synchronised folder
(Dropbox, Google Drive, OneDrive, Qsync, Syncthing) so that it is backed up and available on other computers.
Menu names refer to JRef 0.8.

## 1. Install

### Windows installer

Run `JRef-<version>-Setup.exe`. It installs for the current user in `%LOCALAPPDATA%\Programs\JRef` (no
administrator rights), creates a Start-menu shortcut and carries its own Python, so nothing else is needed.

The installer is built from this repository on Linux or Windows:

```bash
python packaging/windows/build_windows.py      # writes dist/JRef-<version>-Setup.exe
```

The build machine needs Python 3.11+ with PySide6, `py-rattler`, `pip`, Pillow and NSIS (`makensis`); see the
docstring of `build_windows.py` for details.

### From source (Windows, macOS, Linux)

```bash
python -m venv .venv
.venv\Scripts\activate            # macOS/Linux: source .venv/bin/activate
pip install -e ".[gui]"
python -m bibmanager              # or: jref
```

Python 3.11 or later is required. The `gui` extra installs PySide6 and `watchdog` (inbox folder).

### Where JRef keeps its own files

| | Windows | Linux | macOS |
|---|---|---|---|
| Settings (`config.json`) | `%APPDATA%\JRef\` | `~/.config/JRef/` | `~/Library/Application Support/JRef/` |
| Per-device data (device id, database of a shared library) | `%LOCALAPPDATA%\JRef\` | `~/.local/share/JRef/` | `~/Library/Application Support/JRef/data/` |

`BIBMANAGER_CONFIG` points JRef at another settings file and `JREF_DATA_DIR` at another data folder.
Settings of the earlier names (JBibManager, BibManager) are copied over automatically on first start.

## 2. First start: the library folder

The welcome dialog asks for the **library folder** (default `~/JRefLibrary`) and an optional contact e-mail,
which is sent only to Crossref and OpenAlex in the User-Agent header, as their usage etiquette asks.
JRef organises the folder like this:

```
<library folder>/
├── pdfs/<year>/<key>.pdf     one copy of every PDF; supplementary files as <key>_S1.pdf, <key>_S2.xlsx, ...
├── bibtex/master.bib         the whole library
├── bibtex/<project>.bib      one file per project folder, rewritten after every change
├── inbox/                    PDFs saved here are imported automatically
├── sync/                     change journals, only once the library is shared (section 3)
└── library.db                the database; it leaves this folder once the library is shared
```

Useful settings (**Tools ▸ Preferences…**, `Ctrl+,`):

- *When adding PDFs*: **move** them into the library (single copy, default) or **copy** them and leave the
  originals where they are.
- *Inbox folder*: point your browser's download folder at it to import PDFs as you download them; duplicates
  go to `inbox/Duplicates/`, nothing is deleted. Imports whose details could not be confirmed online (no DOI or
  arXiv ID found, or only a title match) wait in the **Inbox** on the shelf until you mark them as verified
  (`Ctrl+Return`).
- *BibTeX*: extra words to keep capitalised in titles (`{...}` protection), on top of the built-in list.

**Writing a project's `.bib` into your manuscript folder.** Select the project ▸ **Project settings** (or
right-click it on the shelf ▸ *Project Settings…*) ▸ *Also write to*, and choose e.g. `references.bib` next to
your `.tex`. The copy in `bibtex/` is always kept as well. **Tools ▸ Rewrite .bib Files** (`F5`) rewrites every
file on demand. Once a project writes next to a manuscript, its header shows the **manuscript check**: the keys
cited in the `.tex` files of that folder, those missing from the project (*add them* adds the ones the library
has) and the project entries never cited (*show* lists them).

**Existing PDFs.** There is no need to copy a collection you already have. *File ▸ Import from Mendeley…*
builds the library from Mendeley's BibTeX export and links the PDFs where they are; *Tools ▸ Find Missing
PDFs…* matches loose PDFs in a folder to entries without a file.

## 3. Keeping the library in a cloud folder

Putting the library folder inside Dropbox, Google Drive, OneDrive, Qsync or Syncthing gives you a backup and
the same library on other computers and on the Android app. One rule matters:

> **A SQLite database must not be synchronised while it is in use.** A sync client can upload `library.db` in
> the middle of a write, and two computers writing it produce "conflicted copies" or a corrupt file.

JRef solves this with **Tools ▸ Share Library with Other Devices…**: the database moves to the per-device data
folder (section 1), and the synced folder receives small append-only change journals in `sync/`, one per
device, that every device replays. We recommend turning sharing on whenever the library folder is synchronised,
even with a single computer. The design is described in [ANDROID.md](../ANDROID.md).

### Step 1: keep the folder on disk, not online-only

Cloud clients can keep files in the cloud and download them only when opened. JRef reads the PDFs (full-text
index, Ask AI, supplementary files) and needs the journals to be present, so keep the whole library folder
available offline:

| Client | What to do |
|---|---|
| Dropbox | In File Explorer, right-click the library folder ▸ **Make available offline**. |
| Google Drive for desktop | *Streaming* (the default) keeps files online and makes them inaccessible while the app is not running. Either switch My Drive to **Mirror files** in the app's preferences, or right-click the library folder ▸ **Offline access ▸ Available offline**. |
| OneDrive | Right-click the library folder ▸ **Always keep on this device**. |
| Qsync, Syncthing | Ordinary two-way folder synchronisation: make sure the folder is fully synchronised, not on demand. |

JRef has not been tested on a streamed Google Drive; mirroring is the safer choice.

### Step 2: share the library

1. Open **Tools ▸ Share Library with Other Devices…**.
2. *Synchronised folder*: choose a folder inside your cloud folder, e.g. `Dropbox\MyLibrary`. If your PDFs are
   already *linked* in a synchronised folder (typical after a Mendeley import), the dialog lists those folders:
   pick one and it becomes the library folder without copying a single PDF.
3. *This device*: a name the other devices will see, e.g. `Office PC`.
4. Press **Share Library**.

The status bar then shows `Shared · n other devices · synced hh:mm`. JRef synchronises every 20 seconds and on
**Tools ▸ Synchronise Devices Now** (`F6`).

### Step 3: a second computer

1. Install the cloud client and let it finish downloading the library folder.
2. Install JRef and, in the welcome dialog, choose **the same folder** as library folder. JRef recognises the
   shared library and builds this computer's own database from the journals.
3. Name the device in **Tools ▸ Share Library with Other Devices…**.

For a phone or tablet, see [ANDROID.md](../ANDROID.md).

### Good to know

- **Settings that stay on each computer**: the *Also write to* path of every project `.bib`, the API keys of
  Ask AI, the inbox settings, the saved searches. Set them on every computer.
- **Linked PDFs outside the library folder** do not travel; the Share dialog tells you how many there are.
- **The same paper added on two computers** before they synchronised: both entries are kept, JRef reports them,
  you delete one. Nothing is merged or deleted automatically.
- **Conflicted copies** that a sync client leaves in `sync/` are ignored; the Share dialog lists them and you
  can delete them.
- **Inbox on several computers** (precaution, not a tested limitation): if two computers run JRef at the same
  time on a shared library, enable *Import PDFs saved into the inbox folder automatically* on one of them only.
- **Do not open one unshared library from two computers.** Share it first.
