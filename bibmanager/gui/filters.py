"""The strip above the reference list: filter chips, sort menu and the Rows / Table switch; and the header of the
current view (project title, .bib state and the manuscript check)."""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterable
from datetime import date
from html import escape

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QAction, QActionGroup
from PySide6.QtWidgets import (
    QButtonGroup,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMenu,
    QPushButton,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ..core.manuscript import ManuscriptCheck
from ..core.models import Paper
from ..core.query import Query, parse_query
from .paper_model import facet_values

# (label, query term) of the on/off chips
TOGGLE_CHIPS: tuple[tuple[str, str], ...] = (
    ("Starred", "is:starred"),
    ("Unread", "is:unread"),
    ("Has PDF", "has:pdf"),
    ("In no project", "-has:project"),
)
# (label, facet kind of paper_model.facet_values) of the chips that open a list of values
FACET_CHIPS: tuple[tuple[str, str], ...] = (("Author", "authors"), ("Venue", "publications"), ("Tag", "tags"))
FACET_MENU_LIMIT = 30


def year_ranges(today: date | None = None) -> list[tuple[str, str]]:
    year = (today or date.today()).year
    decade = year - year % 10
    return [
        ("This year", f"year:{year}"),
        ("Last 5 years", f"year:{year - 4}-"),
        (f"{decade}s", f"year:{decade}-{decade + 9}"),
        (f"{decade - 10}s", f"year:{decade - 10}-{decade - 1}"),
        (f"{decade - 20}s", f"year:{decade - 20}-{decade - 11}"),
        (f"Before {decade - 20}", f"year:<{decade - 20}"),
    ]


class FilterBar(QWidget):
    """Chips combined with AND; emits ``changed`` whenever the filters change."""

    changed = Signal()
    sortChanged = Signal(str)
    viewChanged = Signal(str)  # "rows" | "table"
    restrictCleared = Signal()

    def __init__(
        self,
        papers: Callable[[], Iterable[Paper]],
        sorts: Iterable[tuple[str, str]],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("filterBar")
        self._papers = papers
        self._year: tuple[str, str] | None = None  # (label, query term)
        self._facets: dict[str, str] = {}
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 2, 0, 12)
        row.setSpacing(8)
        self.toggles: dict[str, QToolButton] = {}
        for label, term in TOGGLE_CHIPS:
            chip = self._chip(label)
            chip.setCheckable(True)
            chip.toggled.connect(lambda _on: self.changed.emit())
            chip.setToolTip(f"Show only references matching  {term}")
            self.toggles[term] = chip
            row.addWidget(chip)
        self.year_chip = self._chip("Year", menu=True)
        self.year_menu = QMenu(self.year_chip)
        self.year_chip.setMenu(self.year_menu)
        self.year_menu.aboutToShow.connect(self._fill_year_menu)
        row.addWidget(self.year_chip)
        self.facet_chips: dict[str, QToolButton] = {}
        for label, kind in FACET_CHIPS:
            chip = self._chip(label, menu=True)
            menu = QMenu(chip)
            chip.setMenu(menu)
            menu.aboutToShow.connect(lambda k=kind, m=menu: self._fill_facet_menu(k, m))
            self.facet_chips[kind] = chip
            row.addWidget(chip)
        self.restrict_chip = self._chip("")
        self.restrict_chip.setCheckable(True)
        self.restrict_chip.setChecked(True)
        self.restrict_chip.setToolTip("Click to show every reference again")
        self.restrict_chip.clicked.connect(self._clear_restrict)
        self.restrict_chip.hide()
        row.addWidget(self.restrict_chip)
        self.clear_link = QLabel("<a href='#' style='text-decoration:none'>Clear filters</a>")
        self.clear_link.setObjectName("linkLabel")
        self.clear_link.linkActivated.connect(lambda _l: self.clear())
        self.clear_link.hide()
        row.addWidget(self.clear_link)
        row.addStretch(1)
        self.sort_button = QToolButton()
        self.sort_button.setObjectName("sortButton")
        self.sort_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        sort_menu = QMenu(self.sort_button)
        self.sort_group = QActionGroup(self)
        self.sort_actions: dict[str, QAction] = {}
        for key, label in sorts:
            action = QAction(label, self, checkable=True)
            action.setData(key)
            self.sort_group.addAction(action)
            sort_menu.addAction(action)
            action.triggered.connect(lambda _c=False, k=key: self.sortChanged.emit(k))
            self.sort_actions[key] = action
        self.sort_button.setMenu(sort_menu)
        row.addWidget(self.sort_button)
        segments = QHBoxLayout()
        segments.setSpacing(0)
        self.view_group = QButtonGroup(self)
        self.view_buttons: dict[str, QToolButton] = {}
        for i, (key, label) in enumerate((("rows", "Rows"), ("table", "Table"))):
            button = QToolButton()
            button.setObjectName("segment")
            button.setProperty("first", i == 0)
            button.setProperty("last", i == 1)
            button.setText(label)
            button.setCheckable(True)
            self.view_group.addButton(button, i)
            self.view_buttons[key] = button
            segments.addWidget(button)
            button.clicked.connect(lambda _c=False, k=key: self.viewChanged.emit(k))
        row.addLayout(segments)
        self.changed.connect(self._update_clear)

    @staticmethod
    def _chip(text: str, *, menu: bool = False) -> QToolButton:
        chip = QToolButton()
        chip.setObjectName("chip")
        chip.setText(text)
        chip.setProperty("menu", menu)
        if menu:
            chip.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        chip.setCursor(Qt.CursorShape.PointingHandCursor)
        return chip

    # ------------------------------------------------------------------ state
    def chip_query(self) -> Query:
        terms = [term for term, chip in self.toggles.items() if chip.isChecked()]
        if self._year is not None:
            terms.append(self._year[1])
        return parse_query(" ".join(terms))

    def facets(self) -> dict[str, str]:
        return dict(self._facets)

    def active(self) -> bool:
        return any(c.isChecked() for c in self.toggles.values()) or self._year is not None or bool(self._facets)

    def clear(self) -> None:
        for chip in self.toggles.values():
            chip.blockSignals(True)
            chip.setChecked(False)
            chip.blockSignals(False)
        self._year = None
        self._facets = {}
        self._restyle()
        self.changed.emit()

    def set_toggle(self, term: str, on: bool) -> None:
        self.toggles[term].setChecked(on)

    def set_year(self, label: str | None, term: str | None = None) -> None:
        self._year = (label, term) if label and term else None
        self._restyle()
        self.changed.emit()

    def set_facet(self, kind: str, value: str | None) -> None:
        if value:
            self._facets[kind] = value
        else:
            self._facets.pop(kind, None)
        self._restyle()
        self.changed.emit()

    def set_sort(self, key: str) -> None:
        action = self.sort_actions.get(key)
        if action is not None:
            action.setChecked(True)
            self.sort_button.setText(f"Sort: {action.text()}")

    def set_view(self, mode: str) -> None:
        button = self.view_buttons.get(mode)
        if button is not None:
            button.setChecked(True)

    def show_restrict(self, text: str | None) -> None:
        self.restrict_chip.setText(f"{text}  ✕" if text else "")
        self.restrict_chip.setChecked(True)
        self.restrict_chip.setVisible(bool(text))

    def _clear_restrict(self) -> None:
        self.restrict_chip.hide()
        self.restrictCleared.emit()

    def _restyle(self) -> None:
        self.year_chip.setText(f"Year: {self._year[0]}" if self._year else "Year")
        self.year_chip.setCheckable(True)
        self.year_chip.setChecked(self._year is not None)
        for label, kind in FACET_CHIPS:
            chip = self.facet_chips[kind]
            value = self._facets.get(kind)
            chip.setCheckable(True)
            chip.setChecked(bool(value))
            short = value if not value or len(value) <= 28 else value[:26] + "…"
            chip.setText(f"{label}: {short}" if value else label)
            chip.setToolTip(value or f"Show only references with this {label.lower()}")

    def _update_clear(self) -> None:
        self.clear_link.setVisible(self.active())

    # ------------------------------------------------------------------ menus
    def _fill_year_menu(self) -> None:
        menu = self.year_menu
        menu.clear()
        any_year = menu.addAction("Any year")
        any_year.triggered.connect(lambda _c=False: self.set_year(None))
        menu.addSeparator()
        for label, term in year_ranges():
            action = menu.addAction(label)
            action.setCheckable(True)
            action.setChecked(self._year is not None and self._year[1] == term)
            action.triggered.connect(lambda _c=False, lb=label, tm=term: self.set_year(lb, tm))
        menu.addSeparator()
        menu.addAction("Range...").triggered.connect(lambda _c=False: self._ask_year_range())

    def _ask_year_range(self) -> None:
        text, ok = QInputDialog.getText(self, "Year", "Years (e.g. 2015-2020, 1998, >=2018, -1990):")
        text = text.strip()
        if ok and text:
            self.set_year(text, f"year:{text}")

    def _fill_facet_menu(self, kind: str, menu: QMenu) -> None:
        menu.clear()
        counts = Counter(v for p in self._papers() for v in facet_values(p, kind) if v)
        current = self._facets.get(kind)
        if current:
            menu.addAction(f"Any {dict((k, lb) for lb, k in FACET_CHIPS)[kind].lower()}").triggered.connect(
                lambda _c=False: self.set_facet(kind, None)
            )
            menu.addSeparator()
        if not counts:
            menu.addAction("(none in this view)").setEnabled(False)
            return
        for value, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0].casefold()))[:FACET_MENU_LIMIT]:
            action = menu.addAction(f"{value}   ({n})")
            action.setCheckable(True)
            action.setChecked(value == current)
            action.triggered.connect(lambda _c=False, v=value: self.set_facet(kind, v))
        if len(counts) > FACET_MENU_LIMIT:
            menu.addSeparator()
            menu.addAction(f"All {len(counts)}...").triggered.connect(
                lambda _c=False: self._ask_facet(kind, sorted(counts, key=str.casefold))
            )

    def _ask_facet(self, kind: str, values: list[str]) -> None:
        label = dict((k, lb) for lb, k in FACET_CHIPS)[kind]
        value, ok = QInputDialog.getItem(self, label, f"{label}:", values, 0, True)
        if ok and value:
            self.set_facet(kind, value)


class StatCard(QFrame):
    """One number of the manuscript check, with a sentence and an optional action link."""

    activated = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("statCard")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 10, 14, 10)
        layout.setSpacing(2)
        self.number = QLabel()
        self.number.setObjectName("statNumber")
        self.text = QLabel()
        self.text.setObjectName("statText")
        self.text.setWordWrap(True)
        self.text.linkActivated.connect(lambda _l: self.activated.emit())
        layout.addWidget(self.number)
        layout.addWidget(self.text)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)

    def show_value(self, number: int, text: str, link: str = "", *, warn: bool = False) -> None:
        self.number.setText(f"{number:,}")
        html = escape(text) + (f" · <a href='#' style='text-decoration:none'>{escape(link)}</a>" if link else "")
        self.text.setText(html)
        for widget in (self, self.number):
            widget.setProperty("warn", warn)
            widget.style().unpolish(widget)
            widget.style().polish(widget)


class ViewHeader(QWidget):
    """Title of the current view, its state line, its buttons and (for a project linked to a manuscript folder) the
    manuscript check."""

    addMissingRequested = Signal()
    showUnusedRequested = Signal()
    recheckRequested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("viewHeader")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 18, 0, 12)
        layout.setSpacing(12)
        top = QHBoxLayout()
        titles = QVBoxLayout()
        titles.setSpacing(2)
        self.title = QLabel()
        self.title.setObjectName("listTitle")
        self.subtitle = QLabel()
        self.subtitle.setObjectName("listSubtitle")
        self.subtitle.setTextFormat(Qt.TextFormat.RichText)
        self.subtitle.setWordWrap(True)
        titles.addWidget(self.title)
        titles.addWidget(self.subtitle)
        top.addLayout(titles, 1)
        self.buttons = QHBoxLayout()
        self.buttons.setSpacing(8)
        top.addLayout(self.buttons)
        layout.addLayout(top)
        self.check = QWidget()
        cards = QHBoxLayout(self.check)
        cards.setContentsMargins(0, 0, 0, 0)
        cards.setSpacing(12)
        self.cited = StatCard()
        self.missing = StatCard()
        self.unused = StatCard()
        self.cited.activated.connect(self.recheckRequested)
        self.missing.activated.connect(self.addMissingRequested)
        self.unused.activated.connect(self.showUnusedRequested)
        for card in (self.cited, self.missing, self.unused):
            cards.addWidget(card)
        layout.addWidget(self.check)
        self.check.hide()

    def add_button(self, button: QPushButton) -> None:
        self.buttons.addWidget(button)

    def show_check(self, check: ManuscriptCheck | None, main_file: str = "") -> None:
        if check is None or not check.tex_files:
            self.check.hide()
            return
        files = check.tex_files
        where = main_file or (files[0].name if len(files) == 1 else f"{len(files)} .tex files")
        self.cited.show_value(len(check.cited), f"keys cited in {where}", "check again")
        missing = len(check.missing)
        if missing:
            absent = missing - len(check.missing_in_library)
            text = "cited but missing from the project"
            if absent:
                text += f" ({absent} not in the library)"
            self.missing.show_value(missing, text, "add them" if check.missing_in_library else "", warn=True)
            self.missing.setToolTip("\n".join(check.missing[:40]))
        else:
            self.missing.show_value(0, "cited keys missing from the project")
            self.missing.setToolTip("")
        if check.nocite_all:
            self.unused.show_value(0, "\\nocite{*}: every entry of the .bib file is printed")
        else:
            self.unused.show_value(len(check.unused), "in the project, never cited", "show" if check.unused else "")
        self.unused.setToolTip("\n".join(check.unused[:40]))
        self.check.show()
