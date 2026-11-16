"""JRef's visual identity: design tokens (light and dark), bundled fonts, palette and stylesheet.

The colours come from the logo -- navy ink, teal journal cover, amber bookmark ribbon, cream pages -- and are
fixed (the Windows accent colour is not followed). Titles are set in Source Serif 4, citation keys and BibTeX in
JetBrains Mono (both SIL Open Font License 1.1, bundled in ``gui/fonts``), the interface in the system UI font.
The look is built on Qt's Fusion style so that it renders identically on every platform and in the headless
tests; on Windows the title bar is painted to match through DWM.
"""

from __future__ import annotations

import sys
import tempfile
from dataclasses import dataclass
from functools import cache
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QFontDatabase, QGuiApplication, QPalette
from PySide6.QtWidgets import QApplication, QStyleFactory, QWidget

THEME_MODES = ("system", "light", "dark")
FONTS_DIR = Path(__file__).resolve().parent / "fonts"
SERIF_FALLBACKS = ("Source Serif 4", "Cambria", "Georgia", "Times New Roman", "DejaVu Serif", "Serif")
MONO_FALLBACKS = ("JetBrains Mono", "Cascadia Mono", "Consolas", "DejaVu Sans Mono", "Monospace")


@dataclass(frozen=True)
class Theme:
    dark: bool
    # surfaces and text
    window: str
    card: str
    card_border: str
    divider: str
    text: str
    text_secondary: str
    text_tertiary: str
    hover: str
    pressed: str
    selected: str
    selected_border: str
    control: str
    control_hover: str
    control_border: str
    scroll: str
    chip: str
    # brand colours
    accent: str  # amber: primary actions, focus marks
    accent_hover: str
    accent_text: str
    link: str
    strong: str  # navy: secondary strong buttons, active chips
    strong_hover: str
    strong_text: str
    teal: str
    # the shelf (left column), navy in both themes
    shelf: str
    shelf_text: str
    shelf_muted: str
    shelf_header: str
    shelf_hover: str
    shelf_selected: str
    shelf_card: str
    # citation keys
    key_bg: str
    key_border: str
    key_text: str
    # states
    ok: str
    ok_dot: str
    warning_bg: str
    warning_border: str
    warning: str
    danger: str
    star: str
    icon: str

    @property
    def name(self) -> str:
        return "dark" if self.dark else "light"

    # names used by code written against the earlier (0.7) tokens
    @property
    def control_bottom(self) -> str:
        return self.control_border

    @property
    def warning_text(self) -> str:
        return self.warning


LIGHT = Theme(
    dark=False,
    window="#f6f4ef",
    card="#ffffff",
    card_border="#e4e0d6",
    divider="#efece4",
    text="#1d2330",
    text_secondary="#5a6170",
    text_tertiary="#6b7280",
    hover="rgba(29, 35, 48, 10)",
    pressed="rgba(29, 35, 48, 20)",
    selected="#fbf3e2",
    selected_border="#f1ddb0",
    control="#ffffff",
    control_hover="#faf8f3",
    control_border="#dad5c9",
    scroll="rgba(29, 35, 48, 80)",
    chip="#f1eee7",
    accent="#e9ad43",
    accent_hover="#f0bc5e",
    accent_text="#121a28",
    link="#8a5a12",
    strong="#1b2638",
    strong_hover="#2a3b55",
    strong_text="#ffffff",
    teal="#1fa6c1",
    shelf="#1b2638",
    shelf_text="#e8ecf2",
    shelf_muted="#9aa7ba",
    shelf_header="#8b97ac",
    shelf_hover="rgba(255, 255, 255, 16)",
    shelf_selected="#2a3b55",
    shelf_card="#233149",
    key_bg="#fbf3e2",
    key_border="#f1ddb0",
    key_text="#8a5a12",
    ok="#2f7d4f",
    ok_dot="#4cc38a",
    warning_bg="#fbefd6",
    warning_border="#f1ddb0",
    warning="#7a4e0f",
    danger="#b42318",
    star="#e9ad43",
    icon="#3f4552",
)

DARK = Theme(
    dark=True,
    window="#12161e",
    card="#1a202b",
    card_border="#2a3242",
    divider="#252c3a",
    text="#e8ecf2",
    text_secondary="#aeb7c6",
    text_tertiary="#8b95a7",
    hover="rgba(255, 255, 255, 14)",
    pressed="rgba(255, 255, 255, 24)",
    selected="#2e2718",
    selected_border="#5a4520",
    control="#1f2633",
    control_hover="#252d3b",
    control_border="#343d4f",
    scroll="rgba(255, 255, 255, 90)",
    chip="#252d3b",
    accent="#e9ad43",
    accent_hover="#f0bc5e",
    accent_text="#121a28",
    link="#f0c36a",
    strong="#2a3b55",
    strong_hover="#33476a",
    strong_text="#ffffff",
    teal="#4fd8e6",
    shelf="#0e131c",
    shelf_text="#e8ecf2",
    shelf_muted="#9aa7ba",
    shelf_header="#8b97ac",
    shelf_hover="rgba(255, 255, 255, 14)",
    shelf_selected="#1f2b40",
    shelf_card="#172131",
    key_bg="#2e2718",
    key_border="#5a4520",
    key_text="#f0c36a",
    ok="#6fd39e",
    ok_dot="#4cc38a",
    warning_bg="#3a2f17",
    warning_border="#5a4520",
    warning="#f0c36a",
    danger="#ff8a80",
    star="#f0bc5e",
    icon="#d6dce6",
)

_current: Theme = LIGHT


def current() -> Theme:
    return _current


def qcolor(token: str) -> QColor:
    """Parse ``#rrggbb`` or ``rgba(r, g, b, a)`` (alpha 0-255) into a QColor."""
    token = token.strip()
    if token.startswith("rgba("):
        r, g, b, a = (int(float(x)) for x in token[5:-1].split(","))
        return QColor(r, g, b, a)
    return QColor(token)


def system_prefers_dark() -> bool:
    hints = QGuiApplication.styleHints()
    scheme = getattr(hints, "colorScheme", None)
    return bool(scheme is not None and scheme() == Qt.ColorScheme.Dark)


def capture_system_accent(app: QApplication) -> None:
    """Kept for callers written against 0.7: JRef now uses its own brand colours, not the system accent."""


def resolve(mode: str) -> Theme:
    dark = system_prefers_dark() if mode == "system" else mode == "dark"
    return DARK if dark else LIGHT


# --------------------------------------------------------------------------- fonts
@cache
def load_brand_fonts() -> tuple[str, ...]:
    """Register the bundled fonts once (needs a QGuiApplication); returns the families found."""
    families: list[str] = []
    if not FONTS_DIR.is_dir():
        return ()
    for path in sorted(FONTS_DIR.glob("*.ttf")):
        font_id = QFontDatabase.addApplicationFont(str(path))
        if font_id >= 0:
            families.extend(QFontDatabase.applicationFontFamilies(font_id))
    return tuple(dict.fromkeys(families))


def _first_available(candidates: tuple[str, ...]) -> str:
    load_brand_fonts()
    families = set(QFontDatabase.families())
    for family in candidates:
        if family in families:
            return family
    return candidates[-1]


def serif_family() -> str:
    """Titles: Source Serif 4 (bundled), else a system serif."""
    return _first_available(SERIF_FALLBACKS)


def mono_family() -> str:
    """Citation keys and BibTeX: JetBrains Mono (bundled), else a system monospace font."""
    return _first_available(MONO_FALLBACKS)


def ui_font_family() -> str | None:
    families = set(QFontDatabase.families())
    for family in ("Segoe UI Variable Text", "Segoe UI Variable", "Segoe UI"):
        if family in families:
            return family
    return None


def serif_font(point_size: float, *, bold: bool = False) -> QFont:
    font = QFont(serif_family())
    font.setPointSizeF(point_size)
    if bold:
        font.setWeight(QFont.Weight.DemiBold)
    return font


def mono_font(point_size: float, *, medium: bool = False) -> QFont:
    font = QFont(mono_family())
    font.setPointSizeF(point_size)
    font.setStyleHint(QFont.StyleHint.Monospace)
    if medium:
        font.setWeight(QFont.Weight.Medium)
    return font


def _chevron_file(t: Theme) -> str:
    """Render the combo-box arrow for this theme to a small PNG the stylesheet can reference."""
    from . import icons

    folder = Path(tempfile.gettempdir()) / "jref-theme"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"chevron-{t.icon.lstrip('#')}.png"
    if not path.exists():
        icons.set_theme(t)
        icons.chevron_down().pixmap(24, 24).save(str(path))
    return path.as_posix()


def stylesheet(t: Theme) -> str:
    chevron = _chevron_file(t)
    serif = serif_family()
    mono = mono_family()
    return f"""
* {{ outline: 0; }}
QMainWindow, QDialog, QWidget#central, QWidget#workspace, QWidget#content {{ background: {t.window}; color: {t.text}; }}
QToolTip {{ background: {t.card}; color: {t.text}; border: 1px solid {t.card_border}; border-radius: 6px; padding: 4px 6px; }}

/* ---------------------------------------------------------------- menus */
QMenuBar {{ background: {t.window}; color: {t.text}; padding: 2px 6px 0 6px; }}
QMenuBar::item {{ background: transparent; padding: 4px 10px; border-radius: 6px; }}
QMenuBar::item:selected {{ background: {t.hover}; }}
QMenu {{ background: {t.card}; color: {t.text}; border: 1px solid {t.card_border}; border-radius: 10px; padding: 5px; }}
QMenu::item {{ padding: 6px 26px 6px 10px; border-radius: 6px; margin: 1px 2px; }}
QMenu::item:selected {{ background: {t.selected}; color: {t.text}; }}
QMenu::item:disabled {{ color: {t.text_tertiary}; }}
QMenu::separator {{ height: 1px; background: {t.divider}; margin: 5px 8px; }}
QMenu::icon {{ padding-left: 6px; }}

/* ---------------------------------------------------------------- the shelf (left column) */
QFrame#shelf {{ background: {t.shelf}; border: none; }}
QFrame#shelf QLabel {{ color: {t.shelf_text}; background: transparent; }}
QLabel#brandName {{ font-family: "{serif}"; font-size: 15pt; font-weight: 600; color: {t.shelf_text}; }}
QLabel#brandSubtitle {{ font-size: 8pt; color: {t.shelf_muted}; }}
QTreeView#collections {{ background: transparent; border: none; color: {t.shelf_text}; }}
QTreeView#collections::item {{ background: transparent; border: none; padding: 0; }}
QTreeView#collections::branch, QTreeView#collections::branch:selected, QTreeView#collections::branch:hover {{
    background: transparent;
}}
QTreeView#collections QLineEdit {{
    background: {t.shelf_card}; color: {t.shelf_text}; border: 1px solid {t.accent}; border-radius: 6px; padding: 1px 4px;
}}
QFrame#shelfCard {{ background: {t.shelf_card}; border-radius: 10px; }}
QFrame#shelfCard QLabel {{ color: {t.shelf_muted}; font-size: 8.5pt; }}
QLabel#deviceSync {{ color: {t.shelf_text}; font-weight: 600; }}

/* ---------------------------------------------------------------- top bar and project header */
QWidget#topBar {{ background: {t.window}; border-bottom: 1px solid {t.card_border}; }}
QLabel#breadcrumb {{ color: {t.text_secondary}; }}
QLineEdit#searchBox {{
    background: {t.control}; color: {t.text}; border: 1px solid {t.control_border}; border-radius: 10px;
    padding: 6px 10px; min-height: 22px; selection-background-color: {t.accent}; selection-color: {t.accent_text};
}}
QLineEdit#searchBox:focus {{ border: 1px solid {t.accent}; }}
QLabel#kbd {{
    font-family: "{mono}"; font-size: 8pt; color: {t.text_secondary}; border: 1px solid {t.control_border};
    border-radius: 5px; padding: 0 5px; background: transparent;
}}
QToolButton#primaryCommand, QPushButton#primary {{
    background: {t.accent}; color: {t.accent_text}; border: 1px solid {t.accent}; border-radius: 10px;
    padding: 7px 14px; font-weight: 600;
}}
QToolButton#primaryCommand:hover, QPushButton#primary:hover {{ background: {t.accent_hover}; }}
QToolButton#primaryCommand::menu-button {{ border: none; border-left: 1px solid rgba(18, 26, 40, 60); width: 20px; }}
QToolButton#command {{
    background: {t.control}; color: {t.text}; border: 1px solid {t.control_border}; border-radius: 10px; padding: 6px 9px;
}}
QToolButton#command:hover {{ background: {t.control_hover}; }}
QLabel#listTitle {{ font-family: "{serif}"; font-size: 21pt; font-weight: 600; color: {t.text}; }}
QLabel#listSubtitle {{ color: {t.text_secondary}; }}
QFrame#statCard {{ background: {t.card}; border: 1px solid {t.card_border}; border-radius: 12px; }}
QFrame#statCard[warn="true"] {{ border: 1px solid {t.selected_border}; }}
QLabel#statNumber {{ font-family: "{serif}"; font-size: 16pt; font-weight: 600; color: {t.text}; }}
QLabel#statNumber[warn="true"] {{ color: {t.link}; }}
QLabel#statText {{ color: {t.text_secondary}; }}

/* ---------------------------------------------------------------- filter chips */
QToolButton#chip {{
    background: {t.control}; color: {t.text}; border: 1px solid {t.control_border}; border-radius: 15px;
    padding: 4px 12px; min-height: 18px;
}}
QToolButton#chip:hover {{ background: {t.control_hover}; }}
QToolButton#chip:checked {{ background: {t.strong}; color: {t.strong_text}; border: 1px solid {t.strong}; }}
QToolButton#chip::menu-indicator {{ image: url("{chevron}"); subcontrol-position: right center; width: 10px; right: 8px; }}
QToolButton#chip[menu="true"] {{ padding-right: 24px; }}
QToolButton#segment {{
    background: {t.control}; color: {t.text}; border: 1px solid {t.control_border}; padding: 4px 12px; min-height: 18px;
}}
QToolButton#segment[first="true"] {{ border-top-left-radius: 8px; border-bottom-left-radius: 8px; }}
QToolButton#segment[last="true"] {{ border-top-right-radius: 8px; border-bottom-right-radius: 8px; }}
QToolButton#segment:checked {{ background: {t.strong}; color: {t.strong_text}; border: 1px solid {t.strong}; }}
QToolButton#sortButton {{
    background: {t.control}; color: {t.text}; border: 1px solid {t.control_border}; border-radius: 8px; padding: 4px 10px;
}}
QToolButton#sortButton::menu-indicator {{ image: url("{chevron}"); width: 10px; }}

/* ---------------------------------------------------------------- cards */
QFrame#card {{ background: {t.card}; border: 1px solid {t.card_border}; border-radius: 14px; }}
QFrame#cardDivider {{ background: {t.divider}; max-height: 1px; border: none; }}
QSplitter::handle {{ background: transparent; }}
QSplitter::handle:horizontal {{ width: 14px; }}
QSplitter::handle:vertical {{ height: 8px; }}

/* ---------------------------------------------------------------- reference list */
QWidget#listHeader {{ background: transparent; border-bottom: 1px solid {t.divider}; }}
QLabel#listInfo {{ color: {t.text_secondary}; }}
QTableView#documents {{
    background: transparent; color: {t.text}; border: none; gridline-color: transparent;
    selection-background-color: transparent; selection-color: {t.text};
}}
QTableView#documents::item, QTableView#documents::item:selected, QTableView#documents::item:hover {{
    background: transparent; border: none;
}}
QHeaderView {{ background: transparent; }}
QHeaderView::section {{
    background: transparent; color: {t.text_secondary}; border: none; border-bottom: 1px solid {t.divider};
    padding: 6px 8px; font-weight: 600;
}}
QHeaderView::section:hover {{ background: {t.hover}; }}
QLabel#emptyTitle {{ font-family: "{serif}"; color: {t.text}; font-size: 14pt; font-weight: 600; }}
QLabel#emptyText {{ color: {t.text_secondary}; }}

/* ---------------------------------------------------------------- reference sheet */
QWidget#sheet, QScrollArea#detailsScroll, QWidget#detailsBody, QStackedWidget#details {{ background: transparent; border: none; }}
QLabel#sheetKind {{ color: {t.text_secondary}; font-size: 8pt; font-weight: 600; letter-spacing: 1px; }}
QLabel#fieldLabel {{ color: {t.text_secondary}; }}
QLabel#sectionLabel {{ color: {t.text_secondary}; font-size: 8pt; font-weight: 600; letter-spacing: 1px; padding-top: 12px; }}
QLabel#multiSelection {{ color: {t.text_secondary}; font-size: 11pt; }}
QLabel#chip {{ background: {t.chip}; color: {t.text}; border-radius: 10px; padding: 2px 10px; min-height: 16px; }}
QFrame#projectChip {{ background: {t.strong}; border-radius: 12px; }}
QFrame#projectChip QLabel {{ color: {t.strong_text}; background: transparent; padding-left: 4px; }}
QFrame#projectChip QToolButton {{ background: transparent; border: none; color: {t.strong_text}; padding: 0 2px; }}
QToolButton#addChip {{
    background: transparent; color: {t.text}; border: 1px dashed {t.control_border}; border-radius: 12px; padding: 3px 10px;
}}
QToolButton#addChip:hover {{ background: {t.hover}; }}
QToolButton#addChip::menu-indicator {{ image: none; width: 0; }}
QLabel#linkLabel {{ color: {t.link}; }}
QLabel#monoPath {{ font-family: "{mono}"; font-size: 9pt; color: {t.text}; }}
QFrame#citeCard {{ background: {t.window}; border: 1px solid {t.card_border}; border-radius: 12px; }}
QLabel#citeCode {{
    font-family: "{mono}"; font-size: 10pt; color: {t.text}; background: {t.card}; border: 1px solid {t.card_border};
    border-radius: 8px; padding: 6px 8px;
}}
QLabel#bibtex {{
    font-family: "{mono}"; font-size: 8.5pt; color: {t.text}; background: {t.card}; border: 1px solid {t.card_border};
    border-radius: 8px; padding: 8px;
}}
QFrame#verifyCard {{ background: {t.warning_bg}; border: 1px solid {t.warning_border}; border-radius: 12px; }}
QFrame#verifyCard QLabel {{ color: {t.text}; background: transparent; }}
QLabel#verifyTitle {{ font-weight: 600; }}
QFrame#fileRow {{ background: {t.card}; border: 1px solid {t.card_border}; border-radius: 10px; }}
QLineEdit[inline="true"], QTextEdit[inline="true"] {{
    background: transparent; color: {t.text}; border: 1px solid transparent; border-radius: 6px; padding: 2px 3px;
    selection-background-color: {t.accent}; selection-color: {t.accent_text};
}}
QLineEdit[inline="true"]:hover, QTextEdit[inline="true"]:hover {{ background: {t.hover}; }}
QLineEdit[inline="true"]:focus, QTextEdit[inline="true"]:focus {{
    background: {t.control}; border: 1px solid {t.accent};
}}
QTextEdit#title {{ font-family: "{serif}"; font-size: 16pt; font-weight: 600; }}
QTextEdit#authors {{ font-size: 10.5pt; color: {t.text_secondary}; }}
QLineEdit#journal {{ font-style: italic; }}
QLineEdit#citation_key {{ font-family: "{mono}"; color: {t.key_text}; }}
QTextEdit#notes {{
    background: {t.card}; color: {t.text}; border: 1px solid {t.card_border}; border-radius: 10px; padding: 6px;
}}
QTextEdit#notes:focus {{ border: 1px solid {t.accent}; }}
QTreeWidget#outline {{ background: transparent; color: {t.text}; border: none; }}
QTreeWidget#outline::item {{ padding: 3px 0; border-radius: 6px; }}
QTreeWidget#outline::item:hover {{ background: {t.hover}; }}
QTreeWidget#outline::item:selected {{ background: {t.selected}; color: {t.text}; }}
QWidget#sheetActions {{ background: transparent; border-top: 1px solid {t.divider}; }}

/* ---------------------------------------------------------------- study mode */
QFrame#studyBar {{ background: {t.shelf}; border: none; }}
QFrame#studyBar QLabel {{ color: {t.shelf_text}; background: transparent; }}
QFrame#studyBar QPushButton {{
    background: transparent; color: {t.shelf_text}; border: 1px solid #3a4a66; border-radius: 8px; padding: 5px 12px;
}}
QFrame#studyBar QPushButton:hover {{ background: {t.shelf_hover}; }}
QLabel#keyBadgeStrong {{
    font-family: "{mono}"; color: {t.accent_text}; background: {t.accent}; border-radius: 6px; padding: 2px 8px;
}}
QLabel#studyTitle {{ font-family: "{serif}"; font-size: 19pt; font-weight: 600; color: {t.text}; }}
QTextBrowser#assistantTranscript {{
    background: {t.card}; color: {t.text}; border: 1px solid {t.card_border}; border-radius: 12px; padding: 8px;
}}
QPlainTextEdit#studyNotes, QTextBrowser#studyPreview {{
    background: {t.card}; color: {t.text}; border: 1px solid {t.card_border}; border-radius: 12px; padding: 10px;
    font-family: "{serif}"; font-size: 11pt;
}}

/* ---------------------------------------------------------------- controls */
QPushButton {{
    background: {t.control}; color: {t.text}; border: 1px solid {t.control_border}; border-radius: 9px;
    padding: 6px 14px; min-height: 18px;
}}
QPushButton:hover {{ background: {t.control_hover}; }}
QPushButton:pressed {{ background: {t.hover}; color: {t.text_secondary}; }}
QPushButton:disabled {{ color: {t.text_tertiary}; }}
QPushButton#strong {{ background: {t.strong}; color: {t.strong_text}; border: 1px solid {t.strong}; font-weight: 600; }}
QPushButton#strong:hover {{ background: {t.strong_hover}; }}
QPushButton#subtle {{ background: transparent; border: 1px solid transparent; text-align: left; padding: 5px 8px; }}
QPushButton#subtle:hover {{ background: {t.hover}; }}
QToolButton#iconButton {{ background: transparent; border: 1px solid transparent; border-radius: 8px; padding: 4px; }}
QToolButton#iconButton:hover {{ background: {t.hover}; }}
QComboBox {{
    background: {t.control}; color: {t.text}; border: 1px solid {t.control_border}; border-radius: 8px;
    padding: 4px 10px; min-height: 18px;
}}
QComboBox:hover {{ background: {t.control_hover}; }}
QComboBox::drop-down {{ border: none; width: 22px; }}
QComboBox::down-arrow {{ image: url("{chevron}"); width: 12px; height: 12px; }}
QComboBox QAbstractItemView {{
    background: {t.card}; color: {t.text}; border: 1px solid {t.card_border}; border-radius: 8px;
    selection-background-color: {t.selected}; selection-color: {t.text}; padding: 4px; outline: 0;
}}
QComboBox#typeCombo {{
    background: transparent; border: none; color: {t.text_secondary}; font-size: 8pt; font-weight: 600; padding: 2px 4px;
}}
QLineEdit {{
    background: {t.control}; color: {t.text}; border: 1px solid {t.control_border}; border-radius: 8px;
    padding: 4px 8px; min-height: 18px; selection-background-color: {t.accent}; selection-color: {t.accent_text};
}}
QLineEdit:focus {{ border: 1px solid {t.accent}; }}
QPlainTextEdit, QTextEdit {{ selection-background-color: {t.accent}; selection-color: {t.accent_text}; }}
QGroupBox {{ border: 1px solid {t.card_border}; border-radius: 12px; margin-top: 16px; padding: 12px 10px 10px 10px; background: {t.card}; }}
QGroupBox::title {{ subcontrol-origin: margin; left: 10px; padding: 0 4px; color: {t.text}; font-weight: 600; }}
QCheckBox, QRadioButton, QLabel {{ color: {t.text}; }}
QProgressBar {{ background: {t.divider}; border: none; border-radius: 2px; max-height: 4px; }}
QProgressBar::chunk {{ background: {t.accent}; border-radius: 2px; }}

/* ---------------------------------------------------------------- scroll bars */
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 4px 2px 4px 0; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 0 4px 2px 4px; }}
QScrollBar::handle {{ background: {t.scroll}; border-radius: 3px; }}
QScrollBar::handle:vertical {{ min-height: 32px; margin: 0 2px; }}
QScrollBar::handle:horizontal {{ min-width: 32px; margin: 2px 0; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}
QFrame#shelf QScrollBar::handle {{ background: rgba(255, 255, 255, 60); }}

/* ---------------------------------------------------------------- status bar */
QStatusBar {{ background: {t.window}; color: {t.text_secondary}; border-top: 1px solid {t.card_border}; }}
QStatusBar::item {{ border: none; }}
QStatusBar QLabel {{ color: {t.text_secondary}; padding: 0 6px; }}
"""


def palette(t: Theme) -> QPalette:
    pal = QPalette()
    roles = {
        QPalette.ColorRole.Window: t.window,
        QPalette.ColorRole.WindowText: t.text,
        QPalette.ColorRole.Base: t.card,
        QPalette.ColorRole.AlternateBase: t.window,
        QPalette.ColorRole.Text: t.text,
        QPalette.ColorRole.Button: t.control,
        QPalette.ColorRole.ButtonText: t.text,
        QPalette.ColorRole.ToolTipBase: t.card,
        QPalette.ColorRole.ToolTipText: t.text,
        QPalette.ColorRole.PlaceholderText: t.text_tertiary,
        QPalette.ColorRole.Highlight: t.accent,
        QPalette.ColorRole.HighlightedText: t.accent_text,
        QPalette.ColorRole.Link: t.link,
        QPalette.ColorRole.Light: t.control_hover,
        QPalette.ColorRole.Midlight: t.control_border,
        QPalette.ColorRole.Mid: t.divider,
        QPalette.ColorRole.Dark: t.control_border,
        QPalette.ColorRole.Shadow: "#000000",
    }
    for role, value in roles.items():
        pal.setColor(role, qcolor(value))
    for role in (QPalette.ColorRole.Text, QPalette.ColorRole.WindowText, QPalette.ColorRole.ButtonText):
        pal.setColor(QPalette.ColorGroup.Disabled, role, qcolor(t.text_tertiary))
    return pal


def apply(app: QApplication, mode: str = "system") -> Theme:
    """Apply palette, stylesheet and icon colours for ``mode`` ("system" | "light" | "dark")."""
    global _current
    from . import icons  # local import: icons depend on the active theme

    if app.style().name().lower() != "fusion":
        app.setStyle(QStyleFactory.create("Fusion"))
    load_brand_fonts()
    resolved = resolve(mode if mode in THEME_MODES else "system")
    sheet = stylesheet(resolved)
    if resolved == _current and app.styleSheet() == sheet:
        return _current  # unchanged: avoid re-polishing every widget
    _current = resolved
    family = ui_font_family()
    if family:
        app.setFont(QFont(family, 10))
    app.setPalette(palette(_current))
    icons.set_theme(_current)
    app.setStyleSheet(sheet)
    return _current


def style_window_frame(widget: QWidget, theme: Theme | None = None) -> None:
    """Windows 10/11: dark or light title bar matching the window background (no-op elsewhere)."""
    if sys.platform != "win32":
        return
    theme = theme or _current
    try:
        import ctypes

        hwnd = int(widget.winId())
        dwm = ctypes.windll.dwmapi
        dark = ctypes.c_int(1 if theme.dark else 0)
        for attribute in (20, 19):  # DWMWA_USE_IMMERSIVE_DARK_MODE (19 on early Windows 10 builds)
            if dwm.DwmSetWindowAttribute(hwnd, attribute, ctypes.byref(dark), ctypes.sizeof(dark)) == 0:
                break
        color = qcolor(theme.window)
        colorref = ctypes.c_int(color.red() | (color.green() << 8) | (color.blue() << 16))
        dwm.DwmSetWindowAttribute(hwnd, 35, ctypes.byref(colorref), ctypes.sizeof(colorref))  # DWMWA_CAPTION_COLOR
    except (AttributeError, OSError):  # older Windows: keep the default frame
        pass
