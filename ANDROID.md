# JRef on Android and on several PCs

JRef 0.6 can keep one library identical on several devices — Windows/Linux PCs running the desktop
application and Android phones or tablets running the JRef app — through **a folder that a synchronisation
tool keeps identical on all of them** (Dropbox, Syncthing, QNAP Qsync, Google Drive, ...).

## How it works

A SQLite database must not be written from two devices through a sync tool: the tool produces "conflicted
copy" files, can upload the file in the middle of a transaction, and re-uploads the whole database for every
star you click. JRef therefore shares the library like this:

| In the synchronised folder                      | On each device (never synchronised)              |
|-------------------------------------------------|--------------------------------------------------|
| `pdfs/<year>/<key>.pdf` — the PDFs              | `library.db` — that device's own database        |
| `bibtex/*.bib` — written by the PCs             | (Windows: `%LOCALAPPDATA%\JRef\libraries\<id>\`, |
| `sync/<device>/NNNNNN.jsonl` — change journals  | Linux: `~/.local/share/JRef/...`, Android: the   |
| `sync/library.json` — identity of the library   | app's private storage)                           |

Every device appends its own changes to its own journal and replays the journals of the others. A device only
ever writes its own files, so the sync tool never has two versions of one file. Changes made offline on two
devices are merged when the journals meet:

* each field of a document (title, tags, notes, star, ...) keeps the value written last;
* deleting a document wins over editing it; folder membership is decided per (document, folder);
* a folder created with the same name on two devices is one folder;
* two documents that received the same citation key on two devices: one of them (the same one everywhere)
  gets the next suffix, e.g. `Smith_2020a`. This includes the same paper added on two devices before they
  synchronised: **both copies are kept and JRef tells you** — delete one. Nothing is merged or deleted
  automatically.

PDF files travel with the sync tool, independently of the journals. A document whose PDF has not arrived yet
is listed normally; its PDF button says "PDF not synchronised to this device yet".

## 1. On the PC (once)

1. Install JRef 0.6 (`dist\JRef-0.6.0-Setup.exe`). It replaces JBibManager; settings and library are kept.
2. **Tools ▸ Share Library with Other Devices...** Choose the synchronised folder and press *Share Library*.
   * If your PDFs are *linked* where Mendeley kept them, the dialog lists those folders. Pick the one that is
     (or will be) synchronised: it becomes the library folder. **No PDF is copied or moved**; JRef only adds
     its `pdfs/`, `bibtex/` and `sync/` sub-folders there, and the links become relative to the folder, so
     they work on every device.
   * PDFs linked in a folder *outside* the library folder stay usable on this PC. On another device they
     appear once that folder is synchronised too and mapped in the app (Settings ▸ Linked PDFs).
3. The status bar now shows `Shared · n other devices · synced hh:mm`. JRef synchronises every 20 seconds
   and on **Tools ▸ Synchronise Devices Now** (F6).

A second PC: install JRef, choose the synchronised folder as library folder in the welcome dialog. JRef
recognises the shared library and builds its own database from the journals.

## 2. On the phone

1. Make the folder appear on the phone as a real folder, with **two-way** synchronisation:
   * Dropbox: the official app does not keep folders on the phone; use *Dropsync (Autosync for Dropbox)* or
     *FolderSync*;
   * QNAP Qsync: *Qsync Pro* (folder pair); Syncthing: *Syncthing-Fork*; Google Drive/OneDrive: *FolderSync*
     or the *Autosync* apps.
   A good place is `Internal storage/JRef`. Exclude nothing; let it synchronise `sync/` often (it is tiny).
2. Install the APK (see below) and start JRef:
   * *Allow access to all files* — the library is a folder owned by the sync app;
   * *Choose the folder* — the one that contains `sync/` and `pdfs/`;
   * *Open the library*. The first synchronisation replays the journals (a few seconds for some thousand
     documents).

What the app does: browse All Documents / Recently Added / Recently Read / Favorites / Needs Review /
My Publications / Unsorted and the folders; search with the desktop syntax (`author:anand journal:solids
year:2015-2020 tag:review is:unread`); open the PDF in your PDF reader; star, read/unread, tags, notes,
folders; open supplementary files; edit the details (authors or year changes rename key and PDF as on the desktop); update details
online; copy `\cite{key}` or the BibTeX entry; share a reference; delete.

Adding documents on the phone:
* **+ ▸ PDF from this device** (e.g. from Download), or *Share / Open with ▸ JRef* from the browser or a file
  manager. JRef hashes the file (duplicates are refused), looks for a DOI or arXiv id on the first pages, asks
  Crossref / arXiv (OpenAlex by title otherwise), files the PDF as `pdfs/<year>/<key>.pdf` and journals the
  new document. Opened inside a folder, the document is added to that folder.
* **+ ▸ By DOI or arXiv ID**: a reference without PDF.
The document appears on the PC at its next synchronisation (as soon as the sync app has delivered the files),
in its folder, and the PC rewrites the `.bib` files. The phone never writes `.bib` files.

## 3. Building the APK

The APK is not in this folder: it has to be compiled with the Android SDK/NDK (python-for-android), which
needs Linux. Two ways, both prepared:

* **GitHub (no local tools).** Put this folder in a *private* GitHub repository (`git init`, commit, push),
  and the build starts by itself (every push to `main` builds; *Actions ▸ Android APK ▸ Run workflow* starts one by hand). After 30–45 minutes the finished run offers
  **JRef-android-apk** under *Artifacts*. The workflow is `.github/workflows/android.yml`; it first runs the
  core tests; if the build fails, the run also offers `buildozer-log`.
* **Linux or WSL2.** `bash packaging/android/build_apk.sh` (installs what is needed, builds
  `bin/jref-0.6.0-arm64-v8a-debug.apk`); `... build_apk.sh deploy` also installs it on a phone connected by
  USB. First build: about 4 GB of downloads, 30–60 minutes.

Copy the APK to the phone and open it (allow "install unknown apps" for the file manager). It is
debug-signed: fine for your own devices. Updates: build again and install over the old one (the private
database and settings are kept; even if they were lost, the library is rebuilt from the journals).

The recipe is `buildozer.spec`. Android has no PyMuPDF: the app reads PDFs with `pypdf` (first three pages,
enough for DOI / arXiv id / title), and its database works with or without SQLite FTS5.

## 4. Trying the phone interface on a PC

    pip install kivy pypdf
    python main.py                                   # the real app, on a library folder of this PC
    python packaging/android/demo_walkthrough.py out # scripted tour with screenshots on a throw-away library

`JREF_MOBILE_HOME` sets the folder used as the app's private storage, `JREF_MOBILE_LIBRARY` the library.

## 5. Status of the Android version (0.6.0)

Tested on a PC: all of `bibmanager.core` including the synchronisation (two- and three-device simulations,
random edit/delivery orders), and the complete phone interface through `demo_walkthrough.py` (Kivy runs the
same code on Android).

**Not yet run on a device** — it could not be compiled in the environment where it was written: the APK build
itself and the small Android layer `bibmanager/mobile/droid.py` (the "All files access" request, opening a PDF
in the reader through a MediaStore `content://` URI, receiving PDFs shared by other apps). If one of these
misbehaves on your phone, `adb logcat -s python` shows the Python traceback.

Not in the phone app: Mendeley import, inbox watcher, "Find Missing PDFs", naming-rule re-keying, external
`.bib` targets, full-text search inside PDFs (the PC does these).
