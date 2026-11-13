"""JRef for Android: the phone interface (Kivy) on the same ``bibmanager.core`` as the desktop application.

The library is the folder that a sync app keeps identical with the PC (see ``bibmanager.core.sync``): this
device has its own database in the app's private storage and writes only its own journal into the folder.

Threading: hashing, PDF reading and HTTP run in worker threads (``JRefApp.background``); every ``Library`` call
that touches SQLite runs on the Kivy main thread, as on the desktop.

Run on a PC for development:  ``python main.py``  (``JREF_MOBILE_HOME`` overrides the private data folder).
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any

from kivy.animation import Animation
from kivy.app import App
from kivy.clock import Clock
from kivy.core.clipboard import Clipboard
from kivy.core.text import Label as CoreLabel
from kivy.core.window import Window
from kivy.factory import Factory
from kivy.lang import Builder
from kivy.metrics import dp, sp
from kivy.properties import BooleanProperty, ColorProperty, NumericProperty, ObjectProperty, StringProperty
from kivy.uix.anchorlayout import AnchorLayout
from kivy.uix.behaviors import ButtonBehavior
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.image import Image
from kivy.uix.label import Label
from kivy.uix.modalview import ModalView
from kivy.uix.recycleview.views import RecycleDataViewBehavior
from kivy.uix.screenmanager import NoTransition, Screen, ScreenManager
from kivy.uix.textinput import TextInput

from .. import APP_AUTHOR, APP_AUTHOR_EMAIL, APP_NAME, APP_SUBTITLE, __version__
from ..core import AppConfig, IngestStatus, Library, Paper
from ..core.config import DEFAULT_CONTACT_EMAIL, REPO_CONFIG_PATH
from ..core.extractor import clean_doi, find_arxiv_ids, find_dois
from ..core.query import build_record, matches, parse_query
from ..core.sync import is_shared, sync_dir
from . import droid

log = logging.getLogger(__name__)

HERE = Path(__file__).resolve().parent
ASSETS = HERE / "assets"
SYNC_INTERVAL_S = 20.0
VIEWS = (
    ("all", "All Documents", "all_documents"),
    ("recent", "Recently Added", "recently_added"),
    ("read", "Recently Read", "recently_read"),
    ("favorites", "Favorites", "favorites"),
    ("review", "Needs Review", "needs_review"),
    ("mine", "My Publications", "person"),
    ("unsorted", "Unsorted", "unsorted"),
)
SORTS = (("added", "Date added"), ("author", "First author"), ("title", "Title"), ("year", "Year"))
EDIT_FIELDS = (
    ("title", "Title"),
    ("authors", "Authors   (Family, Given; Family, Given)"),
    ("year", "Year"),
    ("journal", "Journal / container"),
    ("volume", "Volume"),
    ("issue", "Issue"),
    ("pages", "Pages"),
    ("publisher", "Publisher"),
    ("doi", "DOI"),
    ("arxiv_id", "arXiv ID"),
    ("entry_type", "BibTeX type   (article, book, inproceedings ...)"),
    ("citation_key", "Citation key"),
)


# =========================================================================== widgets
ACCENT = (0.082, 0.560, 0.640, 1)


class Icon(Image):
    """A white monoline icon of ``assets/`` tinted by ``color``."""

    name = StringProperty("document")


class IconButton(ButtonBehavior, AnchorLayout):
    icon = StringProperty("document")
    tint = ColorProperty((1, 1, 1, 1))


class Btn(ButtonBehavior, Label):
    filled = BooleanProperty(True)
    tone = ColorProperty(ACCENT)


class Fab(ButtonBehavior, AnchorLayout):
    pass


class PaperRow(RecycleDataViewBehavior, ButtonBehavior, BoxLayout):
    paper_id = NumericProperty(0)
    title = StringProperty("")
    sub = StringProperty("")
    star = BooleanProperty(False)
    unread = BooleanProperty(False)
    pdf = NumericProperty(0)  # 0: no PDF, 1: on this device, 2: recorded but not on this device (yet)
    title_fit = StringProperty("")  # the title on at most two lines, the second one ending in an ellipsis
    _measure: dict[bool, Any] = {}

    def on_release(self) -> None:
        App.get_running_app().show_paper(self.paper_id)

    def on_pdf(self, _row: Any, value: int) -> None:
        if value < 0:  # "has a PDF": whether the file is on this device is looked up for visible rows only
            self.pdf = App.get_running_app().pdf_state(self.paper_id)

    def on_title(self, *_args: Any) -> None:
        self._fit()

    def on_width(self, *_args: Any) -> None:
        self._fit()

    def on_unread(self, *_args: Any) -> None:
        self._fit()

    def _fit(self) -> None:
        """Kivy can shorten ("...") a single line only, so the two lines are laid out here: as many words as
        fit on the first, and the rest cut with an ellipsis on the second."""
        text = " ".join(self.title.split())
        available = self.width - dp(16 + 6 + 8 + 40)
        if available <= dp(40) or not text:
            self.title_fit = text
            return
        bold = bool(self.unread)
        ruler = self._measure.get(bold)
        if ruler is None:
            ruler = self._measure[bold] = CoreLabel(font_size=sp(15.5), bold=bold)
        if ruler.get_extents(text)[0] <= available:
            self.title_fit = text
            return
        words = text.split(" ")
        low, high = 1, len(words) - 1  # words on the first line
        while low < high:
            middle = (low + high + 1) // 2
            if ruler.get_extents(" ".join(words[:middle]))[0] <= available:
                low = middle
            else:
                high = middle - 1
        rest = " ".join(words[low:])
        if ruler.get_extents(rest)[0] > available:
            low_c, high_c = 1, len(rest) - 1  # characters kept on the second line
            while low_c < high_c:
                middle = (low_c + high_c + 1) // 2
                if ruler.get_extents(rest[:middle].rstrip() + "\u2026")[0] <= available:
                    low_c = middle
                else:
                    high_c = middle - 1
            rest = rest[:low_c].rstrip(" ,;:-") + "\u2026"
        self.title_fit = " ".join(words[:low]) + "\n" + rest


class DrawerItem(ButtonBehavior, BoxLayout):
    icon = StringProperty("folder")
    text = StringProperty("")
    count = StringProperty("")
    indent = NumericProperty(0)
    selected = BooleanProperty(False)


class BrowserRow(RecycleDataViewBehavior, ButtonBehavior, BoxLayout):
    icon = StringProperty("folder")
    text = StringProperty("")
    detail = StringProperty("")
    path = StringProperty("")
    is_dir = BooleanProperty(True)

    def on_release(self) -> None:
        App.get_running_app().screen("browser").activate(self.path, self.is_dir)


class SheetItem(ButtonBehavior, BoxLayout):
    icon = StringProperty("document")
    text = StringProperty("")
    danger = BooleanProperty(False)
    checked = BooleanProperty(False)


class Sheet(ModalView):
    """Bottom sheet: a list of actions, or of options that stay open while they are toggled."""

    max_height = NumericProperty(dp(520))

    def __init__(self, title: str = "", **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.max_height = Window.height * 0.78
        if title:
            self.ids.box.add_widget(
                Label(text=title, color=(0.56, 0.60, 0.67, 1), bold=True, font_size=sp(12.5), size_hint_y=None,
                      height=dp(34), halign="left", valign="middle", padding=(dp(20), 0),
                      text_size=(Window.width, dp(34)))  # fmt: skip
            )

    def add(self, text: str, icon: str, action: Callable[[], Any], *, danger: bool = False, checked: bool = False,
            keep_open: bool = False) -> SheetItem:  # fmt: skip
        item = SheetItem(text=text, icon=icon, danger=danger, checked=checked)

        def run(*_args: Any) -> None:
            if not keep_open:
                self.dismiss()
            action()

        item.bind(on_release=run)
        self.ids.box.add_widget(item)
        return item


class Dialog(ModalView):
    """Small centred dialog: title, optional message, optional text field, buttons."""

    def __init__(self, title: str, message: str = "", **kwargs: Any) -> None:
        super().__init__(**kwargs)
        box = self.ids.box
        box.add_widget(_wrapped(title, size=sp(18), bold=True))
        if message:
            box.add_widget(_wrapped(message, size=sp(14.5), color=(0.357, 0.400, 0.478, 1)))
        self.field: TextInput | None = None

    def add_field(self, text: str = "", *, hint: str = "", multiline: bool = False) -> TextInput:
        field = _field(text, hint, multiline)
        self.ids.box.add_widget(field)
        self.field = field
        Clock.schedule_once(lambda _dt: setattr(field, "focus", True), 0.15)
        return field

    def add_buttons(self, ok_text: str, on_ok: Callable[[], Any], *, cancel_text: str = "Cancel", danger: bool = False) -> None:
        row = BoxLayout(size_hint_y=None, height=dp(46), spacing=dp(10))
        cancel = _button(cancel_text, filled=False)
        cancel.bind(on_release=lambda *_a: self.dismiss())
        ok = _button(ok_text, tone=(0.769, 0.216, 0.169, 1) if danger else None)

        def accept(*_args: Any) -> None:
            self.dismiss()
            on_ok()

        ok.bind(on_release=accept)
        row.add_widget(cancel)
        row.add_widget(ok)
        self.ids.box.add_widget(row)


class Drawer(ModalView):
    pass


def _wrapped(text: str, *, size: float, bold: bool = False, color: tuple[float, ...] = (0.106, 0.137, 0.200, 1)) -> Label:
    label = Label(text=text, font_size=size, bold=bold, color=color, size_hint_y=None, halign="left", valign="top")
    label.bind(width=lambda w, value: setattr(w, "text_size", (value, None)))
    label.bind(texture_size=lambda w, value: setattr(w, "height", value[1]))
    return label


def _field(text: str = "", hint: str = "", multiline: bool = False) -> TextInput:
    field = Factory.Field()  # the styled text field of jref.kv
    field.text, field.hint_text, field.multiline = text, hint, multiline
    if multiline:
        field.height = dp(150)
        field.padding = (dp(12), dp(10))
    return field


def _button(text: str, *, filled: bool = True, tone: tuple[float, ...] | None = None) -> Any:
    button = Btn()
    button.text, button.filled = text, filled
    if tone is not None:
        button.tone = tone
    return button


# =========================================================================== screens
class SetupScreen(Screen):
    access = BooleanProperty(False)
    access_text = StringProperty("")
    folder = StringProperty("")
    folder_text = StringProperty("No folder chosen yet.")


class LibraryScreen(Screen):
    pass


class PaperScreen(Screen):
    paper_id = NumericProperty(0)
    key = StringProperty("")
    title = StringProperty("")
    authors = StringProperty("")
    venue = StringProperty("")
    review_text = StringProperty("")
    doi = StringProperty("")
    star = BooleanProperty(False)
    pdf = NumericProperty(0)
    pdf_text = StringProperty("")
    folders = StringProperty("")
    tags = StringProperty("")
    notes = StringProperty("")
    abstract = StringProperty("")
    details = StringProperty("")


class EditScreen(Screen):
    paper_id = NumericProperty(0)


class SettingsScreen(Screen):
    folder = StringProperty("")
    library_text = StringProperty("")
    sync_text = StringProperty("")
    device = StringProperty("")
    links_text = StringProperty("")
    email = StringProperty("")
    about = StringProperty("")


class BrowserScreen(Screen):
    """Folder / PDF chooser on ordinary paths (the app has access to shared storage)."""

    mode = StringProperty("folder")  # 'folder': choose a directory; 'pdf': choose a PDF file
    heading = StringProperty("Choose a folder")
    path = StringProperty("")
    choose_text = StringProperty("Use this folder")
    callback = ObjectProperty(None, allownone=True)

    def open(self, mode: str, start: str, callback: Callable[[str], Any]) -> None:
        self.mode, self.callback = mode, callback
        self.heading = "Choose a folder" if mode == "folder" else "Choose a PDF"
        start_path = Path(start or droid.storage_root())
        while not start_path.is_dir() and start_path != start_path.parent:
            start_path = start_path.parent
        self.show(str(start_path))

    def show(self, path: str) -> None:
        self.path = path
        rows: list[dict[str, Any]] = []
        try:
            entries = sorted(os.scandir(path), key=lambda e: (not e.is_dir(), e.name.casefold()))
        except OSError as exc:
            entries = []
            App.get_running_app().toast(f"Cannot open this folder: {exc.strerror or exc}")
        for entry in entries:
            if entry.name.startswith("."):
                continue
            try:
                if entry.is_dir():
                    rows.append({"icon": "folder", "text": entry.name, "detail": "", "path": entry.path, "is_dir": True})
                elif self.mode == "pdf" and entry.name.lower().endswith(".pdf"):
                    size = entry.stat().st_size
                    detail = f"{size / 1e6:.1f} MB" if size >= 1e5 else f"{max(1, size // 1000)} kB"
                    rows.append({"icon": "pdf", "text": entry.name, "detail": detail, "path": entry.path, "is_dir": False})
            except OSError:
                continue
        self.ids.entries.data = rows
        self.ids.entries.scroll_y = 1
        shared = is_shared(Path(path))
        self.choose_text = "Use this folder  (shared library found)" if shared else "Use this folder"

    def activate(self, path: str, is_dir: bool) -> None:
        if is_dir:
            self.show(path)
        elif self.callback is not None:
            App.get_running_app().go_back()
            self.callback(path)

    def go_up(self) -> None:
        parent = str(Path(self.path).parent)
        if parent != self.path:
            self.show(parent)

    def choose_current(self) -> None:
        if self.callback is not None:
            App.get_running_app().go_back()
            self.callback(self.path)


# =========================================================================== application
class JRefApp(App):
    title = APP_NAME
    kv_file = str(HERE / "jref.kv")  # loaded once by App.run()
    view_title = StringProperty("All Documents")
    count_text = StringProperty("")
    status_text = StringProperty("")
    empty_text = StringProperty("")
    storage_root = StringProperty("")

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.library: Library | None = None
        self.node: tuple[str, int | None] = ("all", None)
        self.sort = "added"
        self.query_text = ""
        self.papers: list[Paper] = []
        self.records: dict[int, Any] = {}
        self.settings: dict[str, Any] = {}
        self.history: list[str] = []
        self.pending_jobs = 0
        self._search_event: Any = None
        self._status_event: Any = None
        self._toast: Label | None = None
        self.add_to_folder: int | None = None
        self._pdf_states: dict[int, int] = {}

    # ------------------------------------------------------------------ small helpers used from the kv file
    def img(self, name: str) -> str:
        return str(ASSETS / f"{name}.png")

    def asset(self, name: str) -> str:
        return str(ASSETS / name)

    def screen(self, name: str) -> Any:
        return self.manager.get_screen(name)

    # ------------------------------------------------------------------ settings (private storage of the app)
    @property
    def home(self) -> Path:
        return Path(os.environ.get("JREF_MOBILE_HOME") or self.user_data_dir)

    def load_settings(self) -> dict[str, Any]:
        try:
            data = json.loads((self.home / "mobile.json").read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def save_settings(self) -> None:
        self.home.mkdir(parents=True, exist_ok=True)
        tmp = self.home / "mobile.json.tmp"
        tmp.write_text(json.dumps(self.settings, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.home / "mobile.json")

    def make_config(self, root: str) -> AppConfig:
        """Desktop defaults (protected words, time-outs) with what differs on a phone: the library is always
        shared, its database is private to this device, PDFs handed over are copied, no ``.bib`` is written."""
        return AppConfig.load(
            REPO_CONFIG_PATH if REPO_CONFIG_PATH.is_file() else None,
            library_root=Path(root),
            import_mode="copy",
            contact_email=self.settings.get("contact_email") or DEFAULT_CONTACT_EMAIL,
            inbox_enabled=False,
            sync_enabled=True,
            device_name=self.settings.get("device_name") or droid.device_label(),
            local_data_dir=self.home / "data",
            write_bib_files=False,
            adopt_folder_database=False,
            linked_path_map=dict(self.settings.get("linked_path_map") or {}),
        )

    # ------------------------------------------------------------------ life cycle
    def build(self) -> ScreenManager:
        self.icon = self.asset("icon.png")
        Window.softinput_mode = "below_target"
        Window.clearcolor = (0.086, 0.118, 0.196, 1)
        self.storage_root = droid.storage_root()
        self.manager = ScreenManager(transition=NoTransition())
        for cls in (SetupScreen, LibraryScreen, PaperScreen, EditScreen, BrowserScreen, SettingsScreen):
            self.manager.add_widget(cls())
        Window.bind(on_keyboard=self._on_key)
        return self.manager

    def on_start(self) -> None:
        self.settings = self.load_settings()
        self.sort = self.settings.get("sort", "added")
        root = os.environ.get("JREF_MOBILE_LIBRARY") or self.settings.get("library_root") or ""
        if root and droid.has_storage_access() and self.open_library(root):
            self.show("library", reset=True)
        else:
            self.show_setup(root)
        droid.on_new_intent(lambda intent: Clock.schedule_once(lambda _dt: self.receive_shared(intent), 0))
        Clock.schedule_once(lambda _dt: self.receive_shared(None), 0.8)
        Clock.schedule_interval(lambda _dt: self.sync_now(), SYNC_INTERVAL_S)

    def on_pause(self) -> bool:
        if self.library is not None:
            try:
                self.library.push_changes()
            except Exception:
                log.exception("Could not journal the changes")
        return True

    def on_resume(self) -> None:
        setup = self.screen("setup")
        if self.manager.current == "setup":
            self.refresh_setup(setup.folder)
        Clock.schedule_once(lambda _dt: self.sync_now(), 0.5)
        Clock.schedule_once(lambda _dt: self.receive_shared(None), 0.8)

    def on_stop(self) -> None:
        if self.library is not None:
            try:
                self.library.push_changes()
                self.library.close()
            except Exception:
                log.exception("Could not close the library")
            self.library = None

    # ------------------------------------------------------------------ navigation
    def show(self, name: str, *, reset: bool = False) -> None:
        if reset:
            self.history = []
        elif self.manager.current != name:
            self.history.append(self.manager.current)
        self.manager.current = name

    def go_back(self) -> bool:
        if not self.history:
            return False
        self.manager.current = self.history.pop()
        if self.manager.current == "library":
            self.refresh_rows()
        elif self.manager.current == "paper":
            self.refresh_paper()
        elif self.manager.current == "settings":
            self.refresh_settings()
        return True

    def _on_key(self, _window: Any, key: int, *_args: Any) -> bool:
        if key != 27:  # Android back button / Esc
            return False
        search = self.screen("library").ids.search
        if self.manager.current == "library" and search.text:
            search.text = ""
            return True
        return self.go_back()

    def toast(self, message: str, seconds: float = 2.6) -> None:
        log.info(message)
        if self._toast is not None:
            Window.remove_widget(self._toast)
        label = Label(text=message, font_size=sp(14), color=(1, 1, 1, 1), size_hint=(None, None), halign="center",
                      valign="middle", padding=(dp(16), dp(10)))  # fmt: skip
        label.text_size = (min(Window.width - dp(48), dp(420)), None)
        label.texture_update()
        label.size = (label.texture_size[0], label.texture_size[1])
        label.pos = ((Window.width - label.width) / 2, dp(96))
        from kivy.graphics import Color, RoundedRectangle

        with label.canvas.before:
            Color(0.10, 0.13, 0.20, 0.94)
            RoundedRectangle(pos=label.pos, size=label.size, radius=[dp(12)])
        Window.add_widget(label)
        self._toast = label
        animation = Animation(opacity=1, duration=seconds) + Animation(opacity=0, duration=0.35)
        animation.bind(on_complete=lambda *_a: Window.remove_widget(label))
        animation.start(label)

    def set_status(self, text: str, seconds: float = 6.0) -> None:
        self.status_text = text
        if self._status_event is not None:
            self._status_event.cancel()
        if text and seconds:
            self._status_event = Clock.schedule_once(lambda _dt: setattr(self, "status_text", ""), seconds)

    def background(self, work: Callable[[], Any], done: Callable[[Any], Any], failed: Callable[[Exception], Any]) -> None:
        """Run ``work`` in a thread (no SQLite of the library in there) and deliver the result on the main thread."""

        def run() -> None:
            try:
                result = work()
            except Exception as exc:  # delivered to the main thread
                log.exception("Background task failed")
                Clock.schedule_once(lambda _dt, e=exc: failed(e), 0)
            else:
                Clock.schedule_once(lambda _dt: done(result), 0)

        threading.Thread(target=run, daemon=True).start()

    # ------------------------------------------------------------------ first run
    def show_setup(self, folder: str = "") -> None:
        self.refresh_setup(folder)
        self.show("setup", reset=True)

    def refresh_setup(self, folder: str = "") -> None:
        setup = self.screen("setup")
        setup.access = droid.has_storage_access()
        setup.access_text = (
            "JRef may read and write the synchronised folder."
            if setup.access
            else "The library is a folder kept by your sync app. Allow JRef to read and write files there "
            "(Android asks on its own settings page; come back afterwards)."
        )
        setup.folder = folder
        if not folder:
            setup.folder_text = "No folder chosen yet."
        elif is_shared(Path(folder)):
            devices = [p for p in sync_dir(Path(folder)).iterdir() if p.is_dir()]
            setup.folder_text = f"{folder}\n\nShared library found ({len(devices)} device{'s' if len(devices) != 1 else ''})."
        else:
            setup.folder_text = (
                f"{folder}\n\nNo shared library here yet. If the PC has not shared its library (Tools > Share "
                "Library with Other Devices) or the folder has not finished synchronising, a new, empty library "
                "is started here."
            )

    def request_access(self) -> None:
        droid.request_storage_access()
        Clock.schedule_once(lambda _dt: self.refresh_setup(self.screen("setup").folder), 1.0)

    def browse(self, mode: str, start: str, callback: Callable[[str], Any]) -> None:
        if not droid.has_storage_access():
            self.toast("Allow access to files first")
            return
        self.screen("browser").open(mode, start, callback)
        self.show("browser")

    def setup_folder_chosen(self, path: str) -> None:
        self.refresh_setup(path)

    def finish_setup(self) -> None:
        folder = self.screen("setup").folder
        if folder and self.open_library(folder):
            self.settings["library_root"] = folder
            self.save_settings()
            self.show("library", reset=True)

    # ------------------------------------------------------------------ library
    def open_library(self, root: str) -> bool:
        try:
            library = Library(self.make_config(root))
        except Exception as exc:
            log.exception("Cannot open the library")
            self.toast(f"Cannot open the library: {exc}", 5)
            return False
        if self.library is not None:
            try:
                self.library.close()
            except Exception:
                pass
        self.library = library
        self.node = ("all", None)
        self.reload()
        self.set_status("Synchronising with the other devices ...", 0)  # shown before the first replay starts
        Clock.schedule_once(lambda _dt: self.sync_now(first=True), 0.3)
        return True

    def _papers_for_node(self) -> list[Paper]:
        lib = self.library
        assert lib is not None
        kind, cid = self.node
        if kind == "folder" and cid is not None:
            return lib.papers(cid)
        loaders = {
            "all": lib.papers,
            "recent": lambda: lib.recently_added(100),
            "read": lambda: lib.recently_read(100),
            "favorites": lib.favorite_papers,
            "review": lib.papers_needing_review,
            "mine": lib.my_publications,
            "unsorted": lib.unassigned_papers,
        }
        return loaders.get(kind, lib.papers)()

    def reload(self) -> None:
        """Read the current view from the database (after any change of content)."""
        if self.library is None:
            return
        kind, cid = self.node
        if kind == "folder":
            folder = self.library.db.get_collection(cid) if cid is not None else None
            if folder is None:
                self.node, kind = ("all", None), "all"
            else:
                self.view_title = folder.name
        if kind != "folder":
            self.view_title = next(label for key, label, _icon in VIEWS if key == kind)
        self.papers = self._papers_for_node()
        folders = self.library.folder_names_by_paper()
        self.records = {p.id: build_record(p, folders.get(p.id, ())) for p in self.papers}
        self._pdf_states = {}
        self.refresh_rows()

    def reload_one(self, paper_id: int) -> None:
        """After a change of one document: refresh only its entry (a full reload is slow with thousands of
        documents on a phone). It stays in the current view until the view is opened again."""
        if self.library is None:
            return
        paper = self.library.db.get_paper(int(paper_id))
        index = next((i for i, p in enumerate(self.papers) if p.id == paper_id), None)
        if paper is None or index is None:
            self.reload()
            return
        self.papers[index] = paper
        names = [c.name for c in self.library.collections_of(paper.id)]
        self.records[paper.id] = build_record(paper, names)
        self._pdf_states.pop(paper.id, None)
        self.refresh_rows()

    def _sorted(self, papers: list[Paper]) -> list[Paper]:
        if self.node[0] in ("recent", "read") and not self.query_text:
            return papers  # these views come ordered by time
        if self.sort == "author":
            return sorted(papers, key=lambda p: ((p.meta.authors[0].family or p.meta.authors[0].literal).casefold()
                                                 if p.meta.authors else "￿", p.meta.year or 0))  # fmt: skip
        if self.sort == "title":
            return sorted(papers, key=lambda p: p.meta.title.casefold())
        if self.sort == "year":
            return sorted(papers, key=lambda p: (-(p.meta.year or 0), p.citation_key.casefold()))
        return sorted(papers, key=lambda p: (p.created_at, p.id), reverse=True)

    def refresh_rows(self) -> None:
        """Filter, sort and show ``self.papers`` (after typing in the search box or changing the order)."""
        if self.library is None:
            return
        query = parse_query(self.query_text)
        shown = [p for p in self.papers if matches(query, self.records[p.id])] if query else list(self.papers)
        rows = []
        for p in self._sorted(shown):
            rows.append(
                {
                    "paper_id": p.id,
                    "title": p.meta.title or p.citation_key,
                    "sub": self._subtitle(p),
                    "star": p.favorite,
                    "unread": not p.is_read,
                    "pdf": -1 if p.has_pdf else 0,  # resolved by the rows that are displayed (pdf_state)
                }
            )
        view = self.screen("library").ids.papers
        view.data = rows
        total = len(self.papers)
        self.count_text = f"{len(rows)} of {total}" if query else f"{total} document{'s' if total != 1 else ''}"
        if rows:
            self.empty_text = ""
        elif query:
            self.empty_text = "Nothing matches the search."
        elif self.node == ("all", None):
            self.empty_text = "The library is empty.\nAdd a PDF with +, or wait for the folder to synchronise."
        else:
            self.empty_text = "No documents here."

    def pdf_state(self, paper_id: int) -> int:
        """1: the PDF is on this device, 2: recorded but not here (yet), 0: none. Cached until the next reload
        (a file test per document is slow on a phone's shared storage)."""
        state = self._pdf_states.get(paper_id)
        if state is None and self.library is not None:
            paper = self.library.db.get_paper(int(paper_id))
            state = 0 if paper is None or not paper.has_pdf else (1 if self.library.pdf_available(paper) else 2)
            self._pdf_states[paper_id] = state
        return state or 0

    @staticmethod
    def _subtitle(p: Paper) -> str:
        names = [a.family or a.literal for a in p.meta.authors]
        if len(names) > 3:
            authors = f"{names[0]} et al."
        else:
            authors = ", ".join(names)
        parts = [authors or "Unknown authors", p.meta.year_label]
        if p.meta.journal:
            parts.append(p.meta.journal)
        return "  ·  ".join(parts)

    def search_changed(self, text: str) -> None:
        self.query_text = text
        if self._search_event is not None:
            self._search_event.cancel()
        self._search_event = Clock.schedule_once(lambda _dt: self.refresh_rows(), 0.25)

    def choose_sort(self) -> None:
        sheet = Sheet("SORT BY")
        for key, label in SORTS:
            sheet.add(label, "check" if key == self.sort else "sort", lambda k=key: self.set_sort(k), checked=key == self.sort)
        sheet.open()

    def set_sort(self, key: str) -> None:
        self.sort = key
        self.settings["sort"] = key
        self.save_settings()
        self.refresh_rows()
        self.screen("library").ids.papers.scroll_y = 1

    def changed(self) -> None:
        """After a local change: journal it right away, so that the sync app can upload it."""
        if self.library is not None:
            try:
                self.library.push_changes()
            except Exception:
                log.exception("Could not journal the change")

    # ------------------------------------------------------------------ navigation drawer
    def open_drawer(self) -> None:
        if self.library is None:
            return
        drawer = Drawer()
        box = drawer.ids["nav"]
        counts = self.library.counts()
        count_keys = {"all": "all", "favorites": "favorites", "review": "review", "mine": "mine", "unsorted": "unsorted"}

        def item(text: str, icon: str, node: tuple[str, int | None] | None, action: Callable[[], Any], *,
                 count: str = "", indent: int = 0) -> None:  # fmt: skip
            row = DrawerItem(text=text, icon=icon, count=count, indent=indent, selected=node == self.node)

            def run(*_args: Any) -> None:
                drawer.dismiss()
                action()

            row.bind(on_release=run)
            box.add_widget(row)

        def heading(text: str) -> None:
            box.add_widget(Label(text=text, color=(0.56, 0.60, 0.67, 1), bold=True, font_size=sp(12), size_hint_y=None,
                                 height=dp(34), halign="left", valign="bottom", padding=(dp(20), dp(4)),
                                 text_size=(Window.width * 0.84, dp(34))))  # fmt: skip

        for key, label, icon in VIEWS:
            count = str(counts.get(count_keys[key], "")) if key in count_keys else ""
            item(label, icon, (key, None), lambda k=key: self.select_node((k, None)), count=count)
        heading("FOLDERS")
        collections = self.library.collections()
        children: dict[int | None, list[Any]] = {}
        for collection in collections:
            children.setdefault(collection.parent_id, []).append(collection)

        def add_level(parent: int | None, depth: int) -> None:
            for collection in children.get(parent, []):
                item(collection.name, "folder", ("folder", collection.id),
                     lambda c=collection.id: self.select_node(("folder", c)),
                     count=str(counts.get(f"folder:{collection.id}", 0)), indent=depth)  # fmt: skip
                add_level(collection.id, depth + 1)

        add_level(None, 0)
        item("New folder ...", "folder_add", None, self.new_folder)
        heading("")
        item("Settings", "settings", None, self.open_settings)
        drawer.open()

    def select_node(self, node: tuple[str, int | None]) -> None:
        self.node = node
        self.reload()
        self.screen("library").ids.papers.scroll_y = 1

    def new_folder(self, then: Callable[[int], Any] | None = None) -> None:
        dialog = Dialog("New folder", "A folder is a collection: a document can be in several folders.")
        field = dialog.add_field(hint="Name")

        def create() -> None:
            assert self.library is not None
            try:
                folder = self.library.create_collection(field.text)
            except ValueError:
                self.toast("Give the folder a name")
                return
            self.changed()
            if then is not None:
                then(folder.id)
            else:
                self.select_node(("folder", folder.id))

        dialog.add_buttons("Create", create)
        dialog.open()

    # ------------------------------------------------------------------ one document
    def show_paper(self, paper_id: int) -> None:
        self.screen("paper").paper_id = paper_id
        if not self.refresh_paper():
            return
        self.screen("paper").ids.scroll.scroll_y = 1
        self.show("paper")

    def current_paper(self) -> Paper | None:
        if self.library is None:
            return None
        return self.library.db.get_paper(int(self.screen("paper").paper_id))

    def refresh_paper(self) -> bool:
        paper = self.current_paper()
        if paper is None or self.library is None:
            if self.manager.current == "paper":
                self.go_back()
            return False
        s, m = self.screen("paper"), paper.meta
        s.key = paper.citation_key
        s.title = m.title or "(no title)"
        s.authors = m.authors_text() or "Unknown authors"
        venue = [m.journal or m.extra_fields.get("booktitle", "") or m.publisher, m.year_label]
        if m.volume:
            venue.append(f"vol. {m.volume}" + (f" ({m.issue})" if m.issue else ""))
        if m.pages:
            venue.append(f"pp. {m.pages}")
        s.venue = ",  ".join(part for part in venue if part)
        s.review_text = "Needs review: details incomplete or unverified" if paper.needs_review else ""
        s.doi = m.doi
        s.star = paper.favorite
        if not paper.has_pdf:
            s.pdf, s.pdf_text = 0, "No PDF attached"
        elif self.library.pdf_available(paper):
            s.pdf, s.pdf_text = 1, "Open PDF"
        elif paper.pdf_linked:
            s.pdf, s.pdf_text = 2, "Linked PDF is not on this device"
        else:
            s.pdf, s.pdf_text = 2, "PDF not synchronised to this device yet"
        s.folders = ",  ".join(c.name for c in self.library.collections_of(paper.id))
        s.tags = paper.tags
        s.notes = paper.notes
        s.abstract = m.abstract
        details = [f"Type: {m.entry_type}", f"Read: {'yes' if paper.is_read else 'no'}"]
        for label, value in (("DOI", m.doi), ("arXiv", m.arxiv_id), ("Keywords", m.keywords), ("ISSN", m.issn),
                             ("ISBN", m.isbn), ("URL", m.url), ("Added", (paper.created_at or "")[:10])):  # fmt: skip
            if value:
                details.append(f"{label}: {value}")
        s.details = "\n".join(details)
        return True

    def _after_paper_change(self, paper_id: int | None = None) -> None:
        self.changed()
        self.refresh_paper()
        self.reload_one(int(paper_id if paper_id is not None else self.screen("paper").paper_id))

    def toggle_favorite(self, paper_id: int) -> None:
        if self.library is None:
            return
        paper = self.library.db.get_paper(int(paper_id))
        if paper is None:
            return
        self.library.update_flags(paper.id, favorite=not paper.favorite)
        self._after_paper_change(paper.id)

    def open_pdf(self) -> None:
        paper = self.current_paper()
        if paper is None or self.library is None:
            return
        path = self.library.pdf_path(paper)
        if path is None or not path.is_file():
            self.toast("The PDF is not on this device")
            return
        if droid.open_file(path):
            self.library.mark_opened(paper.id)
            self._after_paper_change()
        else:
            self.toast("No application could open the PDF")

    def copy_citation(self) -> None:
        paper = self.current_paper()
        if paper is not None:
            Clipboard.copy(f"\\cite{{{paper.citation_key}}}")
            self.toast(f"Copied  \\cite{{{paper.citation_key}}}")

    def copy_bibtex(self) -> None:
        paper = self.current_paper()
        if paper is not None and self.library is not None:
            Clipboard.copy(self.library.bibtex_entry(paper))
            self.toast("BibTeX entry copied")

    def open_doi(self) -> None:
        paper = self.current_paper()
        if paper is not None and paper.meta.doi:
            droid.open_url(f"https://doi.org/{paper.meta.doi}")

    def paper_menu(self) -> None:
        paper = self.current_paper()
        if paper is None:
            return
        sheet = Sheet()
        sheet.add("Edit details", "edit", self.edit_paper)
        sheet.add("Update details online", "sync", self.lookup_online)
        sheet.add("Mark as unread" if paper.is_read else "Mark as read", "check", self.toggle_read)
        sheet.add("Not my publication" if paper.my_publication else "My publication", "person", self.toggle_mine)
        if paper.needs_review:
            sheet.add("Details are correct (reviewed)", "needs_review", self.mark_reviewed)
        sheet.add("Share reference", "share", self.share_paper)
        sheet.add("Delete from library ...", "delete", self.confirm_delete, danger=True)
        sheet.open()

    def toggle_read(self) -> None:
        paper = self.current_paper()
        if paper is not None and self.library is not None:
            self.library.update_flags(paper.id, is_read=not paper.is_read)
            self._after_paper_change()

    def toggle_mine(self) -> None:
        paper = self.current_paper()
        if paper is not None and self.library is not None:
            self.library.update_flags(paper.id, my_publication=not paper.my_publication)
            self._after_paper_change()

    def mark_reviewed(self) -> None:
        paper = self.current_paper()
        if paper is not None and self.library is not None:
            self._guarded(lambda: self.library.update_metadata(paper.id, needs_review=False))

    def share_paper(self) -> None:
        paper = self.current_paper()
        if paper is None or self.library is None:
            return
        m = paper.meta
        text = f"{m.authors_text()} ({m.year_label}). {m.title}. {m.journal}".strip()
        if m.doi:
            text += f" https://doi.org/{m.doi}"
        if not droid.share_text(text, m.title):
            Clipboard.copy(text)
            self.toast("Reference copied")

    def confirm_delete(self) -> None:
        paper = self.current_paper()
        if paper is None:
            return
        extra = " and its PDF" if paper.pdf_managed else ""
        dialog = Dialog("Delete from library?", f"{paper.citation_key}{extra} will be removed from the library on "
                        "every device. Linked PDFs are never deleted.")  # fmt: skip

        def delete() -> None:
            assert self.library is not None
            self.library.delete_paper(paper.id)
            self.changed()
            self.go_back()
            self.reload()
            self.toast(f"{paper.citation_key} deleted")

        dialog.add_buttons("Delete", delete, danger=True)
        dialog.open()

    def _guarded(self, action: Callable[[], Any]) -> bool:
        try:
            action()
        except Exception as exc:  # LibraryError, DuplicateDoiError, ValueError ...: shown, nothing changed
            self.toast(str(exc), 4.5)
            return False
        self._after_paper_change()
        return True

    def edit_tags(self) -> None:
        paper = self.current_paper()
        if paper is None:
            return
        dialog = Dialog("Tags", "Separate tags with semicolons.")
        field = dialog.add_field(paper.tags, hint="fracture; to cite")
        dialog.add_buttons("Save", lambda: self._guarded(lambda: self.library.update_flags(paper.id, tags=field.text)))
        dialog.open()

    def edit_notes(self) -> None:
        paper = self.current_paper()
        if paper is None:
            return
        dialog = Dialog("Notes")
        field = dialog.add_field(paper.notes, hint="Your notes on this document", multiline=True)
        dialog.add_buttons("Save", lambda: self._guarded(lambda: self.library.update_flags(paper.id, notes=field.text)))
        dialog.open()

    def edit_folders(self) -> None:
        paper = self.current_paper()
        if paper is None or self.library is None:
            return
        library = self.library
        member = {c.id for c in library.collections_of(paper.id)}
        sheet = Sheet("FOLDERS OF THIS DOCUMENT")

        def toggle(item: SheetItem, cid: int) -> None:
            if cid in member:
                library.remove_from_collection(paper.id, cid)
                member.discard(cid)
            else:
                library.add_to_collection(paper.id, cid)
                member.add(cid)
            item.checked, item.icon = cid in member, "check" if cid in member else "folder"

        for collection in library.collections():
            checked = collection.id in member
            row = sheet.add(collection.name, "check" if checked else "folder", lambda: None, checked=checked, keep_open=True)
            row.bind(on_release=lambda item, cid=collection.id: toggle(item, cid))

        def add_new() -> None:
            self.new_folder(then=lambda cid: (library.add_to_collection(paper.id, cid), self._after_paper_change()))

        sheet.add("New folder ...", "folder_add", add_new)
        sheet.bind(on_dismiss=lambda *_a: self._after_paper_change())
        sheet.open()

    # ------------------------------------------------------------------ editing the details
    def edit_paper(self) -> None:
        paper = self.current_paper()
        if paper is None:
            return
        screen = self.screen("edit")
        screen.paper_id = paper.id
        form = screen.ids.form
        form.clear_widgets()
        self._edit_inputs: dict[str, TextInput] = {}
        self._edit_original: dict[str, str] = {}
        m = paper.meta
        values = {
            "title": m.title, "authors": m.authors_inverted(), "year": "" if m.year is None else str(m.year),
            "journal": m.journal, "volume": m.volume, "issue": m.issue, "pages": m.pages, "publisher": m.publisher,
            "doi": m.doi, "arxiv_id": m.arxiv_id, "entry_type": m.entry_type, "citation_key": paper.citation_key,
        }  # fmt: skip
        for name, label in EDIT_FIELDS:
            form.add_widget(Label(text=label, color=(0.56, 0.60, 0.67, 1), font_size=sp(12.5), size_hint_y=None,
                                  height=dp(26), halign="left", valign="bottom",
                                  text_size=(Window.width - dp(32), dp(26))))  # fmt: skip
            field = _field(values[name], multiline=name in ("title", "authors"))
            if name in ("title", "authors"):
                field.height = dp(84)
            if name == "year":
                field.input_filter = "int"
            form.add_widget(field)
            Clock.schedule_once(lambda _dt, f=field: setattr(f, "cursor", (0, 0)), 0)  # show the beginning
            self._edit_inputs[name] = field
            self._edit_original[name] = values[name]
        self.show("edit")

    def save_edit(self) -> None:
        if self.library is None:
            return
        paper_id = int(self.screen("edit").paper_id)
        changes: dict[str, Any] = {}
        for name, field in self._edit_inputs.items():
            text = " ".join(field.text.split())
            if text != " ".join(self._edit_original[name].split()):
                changes[name] = text
        if "citation_key" in changes and not changes["citation_key"]:
            del changes["citation_key"]
        if changes:
            try:
                self.library.update_metadata(paper_id, **changes)
            except Exception as exc:
                self.toast(str(exc), 4.5)
                return
            self.changed()
            self.reload()
        self.go_back()

    def lookup_online(self) -> None:
        paper = self.current_paper()
        if paper is None or self.library is None:
            return
        resolver, m = self.library.resolver, paper.meta
        self.set_status("Looking up details online ...", 0)

        def done(found: tuple[Any, str]) -> None:
            meta, source = found
            self.set_status("")
            if meta is None:
                self.toast("Nothing found online for this document")
            elif self._guarded(lambda: self.library.apply_metadata(paper.id, meta)):
                self.toast(f"Details updated from {source}")

        self.background(
            lambda: resolver.lookup(doi=m.doi, arxiv_id=m.arxiv_id, title=m.title),
            done,
            lambda exc: (self.set_status(""), self.toast(f"Look-up failed: {exc}", 4)),
        )

    # ------------------------------------------------------------------ adding documents
    def add_menu(self) -> None:
        if self.library is None:
            return
        self.add_to_folder = self.node[1] if self.node[0] == "folder" else None
        where = f"  (into {self.view_title})" if self.add_to_folder is not None else ""
        sheet = Sheet("ADD TO THE LIBRARY" + where.upper())
        sheet.add("PDF from this device ...", "pdf", lambda: self.browse("pdf", droid.download_dir(), self.import_pdf))
        sheet.add("By DOI or arXiv ID ...", "open_link", self.add_by_identifier)
        sheet.open()

    def import_pdf(self, path: str, *, delete_source: bool = False) -> None:
        if self.library is None:
            return
        library, folder = self.library, self.add_to_folder
        _resolver = library.resolver  # created on the main thread; used by the worker
        self.pending_jobs += 1
        self.set_status(f"Reading {Path(path).name} ...", 0)

        def done(candidate: Any) -> None:
            self.pending_jobs -= 1
            try:
                result = library.commit_import(candidate, folder, mode="move" if delete_source else "copy")
            except Exception as exc:
                self.set_status("")
                self.toast(f"Import failed: {exc}", 5)
                return
            if delete_source and result.status is not IngestStatus.IMPORTED:
                Path(path).unlink(missing_ok=True)
            self.changed()
            self.reload()
            self.set_status("" if not self.pending_jobs else self.status_text, 0 if self.pending_jobs else 6)
            key = result.paper.citation_key
            if result.status is IngestStatus.IMPORTED:
                extra = "  — check its details" if result.paper.needs_review else ""
                self.toast(f"Added {key}{extra}", 3.5)
                self.show_paper(result.paper.id)
            else:
                self.toast(f"Already in the library as {key}", 3.5)

        def failed(exc: Exception) -> None:
            self.pending_jobs -= 1
            self.set_status("")
            self.toast(f"Could not read the PDF: {exc}", 5)

        self.background(lambda: library.prepare_import(path, check_duplicate=True), done, failed)

    def add_by_identifier(self) -> None:
        dialog = Dialog("Add by DOI or arXiv ID", "The details are fetched from Crossref or arXiv; a PDF can be "
                        "attached later on the PC.")  # fmt: skip
        field = dialog.add_field(hint="10.1016/j.jmps.2020.104159   or   2301.01234")

        def fetch() -> None:
            assert self.library is not None
            text = field.text.strip()
            dois, arxiv = find_dois(text), find_arxiv_ids(text)
            doi = dois[0] if dois else (clean_doi(text) if text.startswith("10.") else "")
            arxiv_id = "" if doi else (arxiv[0] if arxiv else text)
            if not text:
                return
            library, folder, resolver = self.library, self.add_to_folder, self.library.resolver
            self.set_status("Fetching the details ...", 0)

            def done(found: tuple[Any, str]) -> None:
                meta, _source = found
                self.set_status("")
                if meta is None:
                    self.toast("Nothing found for this identifier", 4)
                    return
                result = library.add_reference(meta, folder)
                self.changed()
                self.reload()
                known = result.status is not IngestStatus.IMPORTED
                self.toast(("Already in the library as " if known else "Added ") + result.paper.citation_key, 3.5)
                self.show_paper(result.paper.id)

            self.background(
                lambda: resolver.lookup(doi=doi, arxiv_id=arxiv_id),
                done,
                lambda exc: (self.set_status(""), self.toast(f"Look-up failed: {exc}", 4)),
            )

        dialog.add_buttons("Fetch", fetch)
        dialog.open()

    def receive_shared(self, intent: Any) -> None:
        """PDFs sent by another app ("Share" / "Open with"): copied to the private cache, then imported."""
        if self.library is None:
            return
        for path in droid.incoming_pdfs(droid.cache_dir(str(self.home)), intent):
            self.add_to_folder = None
            self.import_pdf(str(path), delete_source=True)

    # ------------------------------------------------------------------ synchronisation
    def sync_now(self, *, manual: bool = False, first: bool = False) -> None:
        library = self.library
        if library is None or library.sync is None or self.pending_jobs:
            return
        started = time.time()
        try:
            report = library.sync_now()
        except Exception as exc:
            log.exception("Synchronisation failed")
            if manual:
                self.toast(f"Synchronisation failed: {exc}", 5)
            return
        if report.changed:
            self.reload()
            if self.manager.current == "paper":
                self.refresh_paper()
            self.set_status(f"Synchronised: {report.summary()}")
        elif manual:
            self.toast(f"Synchronised: {report.summary().lower()}")
        if report.duplicates:
            a, b = report.duplicates[0]
            self.toast(f"Added on two devices: {a} and {b}. Both were kept; delete one if they are the same.", 6)
        if first and not report.devices:
            self.set_status("No other device in this folder yet: share the library on the PC", 10)
        elif first and not report.changed:
            self.set_status("")
        log.debug("sync took %.2fs", time.time() - started)
        if self.manager.current == "settings":
            self.refresh_settings()

    # ------------------------------------------------------------------ settings
    def open_settings(self) -> None:
        self.refresh_settings()
        self.show("settings")

    def refresh_settings(self) -> None:
        library = self.library
        if library is None or library.sync is None:
            return
        s, engine = self.screen("settings"), library.sync
        s.folder = str(library.root)
        total = library.db.count_papers()
        with_pdf = int(library.db.conn.execute("SELECT COUNT(*) FROM papers WHERE relative_pdf_path IS NOT NULL").fetchone()[0])
        s.library_text = f"{total} documents, {with_pdf} with a PDF"
        lines = []
        for device in engine.devices():
            when = datetime.fromtimestamp(device.last_change).strftime("%d %b %H:%M") if device.last_change else "no changes yet"
            lines.append(f"{'This device' if device.is_self else device.name}  ·  {device.app}  ·  {when}")
        last = datetime.fromtimestamp(engine.last_sync).strftime("%H:%M:%S") if engine.last_sync else "not yet"
        waiting = engine.last_report.waiting
        lines.append(f"Last check: {last}" + (f"  ·  {waiting} changes wait for files still to arrive" if waiting else ""))
        lines.append("The files themselves are carried by your sync app: keep it running for this folder.")
        s.sync_text = "\n".join(lines)
        s.device = engine.device_name
        s.email = self.settings.get("contact_email", "")
        s.about = (
            f"{APP_NAME} {__version__} — {APP_SUBTITLE}\nAuthor: {APP_AUTHOR}  ·  {APP_AUTHOR_EMAIL}\n"
            "Local, file-first reference manager for LaTeX writing."
        )
        box = s.ids.links
        box.clear_widgets()
        unresolved = library.unresolved_link_folders(limit=6)
        mapped = library.config.linked_path_map
        if not unresolved and not mapped:
            s.links_text = "Every linked PDF is inside the library folder, or there are none."
        else:
            s.links_text = (
                "Some PDFs are linked in folders of the PC outside the library folder. If such a folder is "
                "synchronised to this phone too, tell JRef where it is here."
            )
        for folder, count in unresolved:
            button = _button(f"{folder}   ({count} PDFs)  →  choose ...", filled=False)
            button.font_size = sp(13)
            button.bind(on_release=lambda _b, f=folder: self.browse("folder", self.storage_root,
                                                                    lambda local, f=f: self.map_folder(f, local)))  # fmt: skip
            box.add_widget(button)
        for folder, local in mapped.items():
            box.add_widget(_wrapped(f"{folder}\n→ {local}", size=sp(13), color=(0.357, 0.400, 0.478, 1)))

    def map_folder(self, recorded: str, local: str) -> None:
        if self.library is None:
            return
        mapping = dict(self.library.config.linked_path_map)
        mapping[recorded] = local
        self.library.config.linked_path_map = mapping
        self.settings["linked_path_map"] = mapping
        self.save_settings()
        self.reload()
        self.refresh_settings()

    def change_library_folder(self, path: str) -> None:
        if self.library is not None and Path(path) == Path(self.library.root):
            return
        if self.open_library(path):
            self.settings["library_root"] = path
            self.save_settings()
            self.show("library", reset=True)

    def edit_device_name(self) -> None:
        assert self.library is not None and self.library.sync is not None
        dialog = Dialog("Name of this device", "Shown to the other devices that share the library.")
        field = dialog.add_field(self.library.sync.device_name)

        def save() -> None:
            name = " ".join(field.text.split())
            if name and self.library is not None:
                self.settings["device_name"] = name
                self.save_settings()
                root = str(self.library.root)
                self.library.config = replace(self.library.config, device_name=name)
                if self.open_library(root):
                    self.refresh_settings()

        dialog.add_buttons("Save", save)
        dialog.open()

    def edit_email(self) -> None:
        dialog = Dialog("Contact e-mail", "Sent to Crossref and OpenAlex with look-ups (their etiquette for faster service).")
        field = dialog.add_field(self.settings.get("contact_email", ""), hint="name@example.org")

        def save() -> None:
            self.settings["contact_email"] = field.text.strip()
            self.save_settings()
            if self.library is not None:
                self.library.config.contact_email = field.text.strip() or DEFAULT_CONTACT_EMAIL
                if self.library._resolver is not None:
                    self.library._resolver.user_agent = self.library.config.user_agent
            self.refresh_settings()

        dialog.add_buttons("Save", save)
        dialog.open()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    JRefApp().run()


if __name__ == "__main__":
    main()
