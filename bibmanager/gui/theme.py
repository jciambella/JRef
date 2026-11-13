"""Windows 11 (Fluent) inspired design tokens, light and dark, applied as palette + stylesheet.

The look is built on Qt's Fusion style so that it renders identically on every platform (and in
the headless tests); on Windows it additionally follows the system accent colour, the light/dark
setting and paints a matching title bar through DWM.
"""

from __future__ import annotations

import sys
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QFontDatabase, QGuiApplication, QPalette
from PySide6.QtWidgets import QApplication, QStyleFactory, QWidget

THEME_MODES = ("system", "light", "dark")


@dataclass(frozen=True)
class Theme:
    dark: bool
    accent: str
    accent_hover: str
    accent_text: str
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
    control: str
    control_hover: str
    control_border: str
    control_bottom: str
    scroll: str
    chip: str
    warning_bg: str
    warning_border: str
    warning: str
    danger: str
    star: str
    icon: str

    @property
    def name(self) -> str:
        return "dark" if self.dark else "light"


LIGHT = Theme(
    dark=False,
    accent="#005fb8",
    accent_hover="#196ebf",
    accent_text="#ffffff",
    window="#f3f3f3",
    card="#ffffff",
    card_border="#e5e5e5",
    divider="#ebebeb",
    text="#1b1b1b",
    text_secondary="#5c5c5c",
    text_tertiary="#8b8b8b",
    hover="rgba(0, 0, 0, 10)",
    pressed="rgba(0, 0, 0, 18)",
    selected="rgba(0, 0, 0, 15)",
    control="#ffffff",
    control_hover="#f9f9f9",
    control_border="#e1e1e1",
    control_bottom="#868686",
    scroll="rgba(0, 0, 0, 90)",
    chip="#eeeeee",
    warning_bg="#fff4ce",
    warning_border="#f0dc9b",
    warning="#9d5d00",
    danger="#c42b1c",
    star="#e8a317",
    icon="#404040",
)

DARK = Theme(
    dark=True,
    accent="#60cdff",
    accent_hover="#56b9e6",
    accent_text="#000000",
    window="#202020",
    card="#2b2b2b",
    card_border="#363636",
    divider="#3a3a3a",
    text="#ffffff",
    text_secondary="#cfcfcf",
    text_tertiary="#9e9e9e",
    hover="rgba(255, 255, 255, 15)",
    pressed="rgba(255, 255, 255, 10)",
    selected="rgba(255, 255, 255, 22)",
    control="#2d2d2d",
    control_hover="#323232",
    control_border="#3c3c3c",
    control_bottom="#9a9a9a",
    scroll="rgba(255, 255, 255, 100)",
    chip="#3b3b3b",
    warning_bg="#433519",
    warning_border="#5e4b20",
    warning="#fce100",
    danger="#ff99a4",
    star="#f7c325",
    icon="#d6d6d6",
)

_current: Theme = LIGHT
_system_accent: QColor | None = None


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
    """Remember the platform accent colour before Fusion's palette replaces it (Windows only)."""
    global _system_accent
    role = getattr(QPalette.ColorRole, "Accent", None)
    if sys.platform == "win32" and role is not None:
        color = app.palette().color(role)
        # Ignore greys/near-white values (e.g. no accent reported): they would make primary buttons invisible.
        usable = color.isValid() and color.hsvSaturationF() > 0.25 and 0.2 < color.valueF() < 0.98
        _system_accent = color if usable else None


def _with_accent(theme: Theme, accent: QColor | None) -> Theme:
    if accent is None:
        return theme
    if theme.dark:  # Windows uses a lighter tint of the accent on dark surfaces
        hue, sat, light = accent.hslHueF(), accent.hslSaturationF(), accent.lightnessF()
        accent = QColor.fromHslF(max(hue, 0.0), sat, max(light, 0.68), 1.0)
    text = "#000000" if accent.lightnessF() > 0.6 else "#ffffff"
    return replace(theme, accent=accent.name(), accent_hover=accent.lighter(110).name(), accent_text=text)


def resolve(mode: str) -> Theme:
    dark = system_prefers_dark() if mode == "system" else mode == "dark"
    return _with_accent(DARK if dark else LIGHT, _system_accent)


def ui_font_family() -> str | None:
    families = set(QFontDatabase.families())
    for family in ("Segoe UI Variable Text", "Segoe UI Variable", "Segoe UI"):
        if family in families:
            return family
    return None


def _chevron_file(t: Theme) -> str:
    """Render the combo-box arrow for this theme to a small PNG the stylesheet can reference."""
    from . import icons

    folder = Path(tempfile.gettempdir()) / "bibmanager-theme"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"chevron-{t.icon.lstrip('#')}.png"
    if not path.exists():
        icons.set_theme(t)
        icons.chevron_down().pixmap(24, 24).save(str(path))
    return path.as_posix()


def stylesheet(t: Theme) -> str:
    chevron = _chevron_file(t)
    return f"""
* {{ outline: 0; }}
QMainWindow, QDialog, QWidget#central {{ background: {t.window}; color: {t.text}; }}
QToolTip {{ background: {t.card}; color: {t.text}; border: 1px solid {t.card_border}; border-radius: 4px; padding: 4px 6px; }}

/* ---------------------------------------------------------------- menus */
QMenuBar {{ background: {t.window}; color: {t.text}; padding: 2px 6px 0 6px; }}
QMenuBar::item {{ background: transparent; padding: 4px 10px; border-radius: 4px; }}
QMenuBar::item:selected {{ background: {t.hover}; }}
QMenu {{ background: {t.card}; color: {t.text}; border: 1px solid {t.card_border}; border-radius: 8px; padding: 4px; }}
QMenu::item {{ padding: 6px 24px 6px 10px; border-radius: 4px; margin: 1px 2px; }}
QMenu::item:selected {{ background: {t.selected}; }}
QMenu::item:disabled {{ color: {t.text_tertiary}; }}
QMenu::separator {{ height: 1px; background: {t.divider}; margin: 4px 8px; }}
QMenu::icon {{ padding-left: 6px; }}

/* ---------------------------------------------------------------- command bar */
QToolBar#commandBar {{ background: {t.window}; border: none; padding: 4px 10px 6px 10px; spacing: 4px; }}
QToolButton#command {{
    background: transparent; color: {t.text}; border: 1px solid transparent; border-radius: 6px;
    padding: 5px 9px;
}}
QToolButton#command:hover {{ background: {t.hover}; }}
QToolButton#command:pressed {{ background: {t.pressed}; }}
QToolButton#command:disabled {{ color: {t.text_tertiary}; }}
QToolButton#primaryCommand {{
    background: {t.accent}; color: {t.accent_text}; border: 1px solid {t.accent}; border-radius: 6px;
    padding: 5px 10px; font-weight: 600;
}}
QToolButton#primaryCommand:hover {{ background: {t.accent_hover}; }}
QToolButton#primaryCommand::menu-button {{ border: none; border-left: 1px solid {t.accent_hover}; width: 18px; }}
QToolButton#command::menu-button {{ border: none; width: 16px; }}
QFrame#commandSeparator {{ color: {t.divider}; background: {t.divider}; max-width: 1px; margin: 6px 4px; }}
QLineEdit#searchBox {{
    background: {t.control}; color: {t.text}; border: 1px solid {t.control_border};
    border-bottom: 1px solid {t.control_bottom}; border-radius: 6px; padding: 5px 8px; min-height: 20px;
    selection-background-color: {t.accent}; selection-color: {t.accent_text};
}}
QLineEdit#searchBox:hover {{ background: {t.control_hover}; }}
QLineEdit#searchBox:focus {{ border-bottom: 2px solid {t.accent}; padding-bottom: 4px; }}

/* ---------------------------------------------------------------- cards */
QFrame#card {{ background: {t.card}; border: 1px solid {t.card_border}; border-radius: 8px; }}
QFrame#cardDivider {{ background: {t.divider}; max-height: 1px; border: none; }}
QSplitter::handle {{ background: transparent; }}
QSplitter::handle:horizontal {{ width: 6px; }}
QSplitter::handle:vertical {{ height: 6px; }}

/* ---------------------------------------------------------------- navigation */
QTreeView#collections {{ background: transparent; border: none; color: {t.text}; }}
QTreeView#collections::item {{ background: transparent; border: none; padding: 0; }}
QTreeView#collections::branch, QTreeView#collections::branch:selected, QTreeView#collections::branch:hover {{
    background: transparent;
}}
QTreeView#collections QLineEdit {{
    background: {t.control}; color: {t.text}; border: 1px solid {t.control_border};
    border-bottom: 2px solid {t.accent}; border-radius: 4px; padding: 1px 4px;
}}
QComboBox#facetCombo {{
    background: transparent; color: {t.text_secondary}; border: none; border-radius: 4px;
    padding: 5px 8px; font-weight: 600;
}}
QComboBox#facetCombo:hover {{ background: {t.hover}; }}
QWidget#facetPane {{ background: transparent; border-top: 1px solid {t.divider}; }}
QListWidget#facetList {{ background: transparent; border: none; color: {t.text}; }}
QListWidget#facetList::item {{ padding: 4px 8px; border-radius: 4px; margin: 0 4px; }}
QListWidget#facetList::item:hover {{ background: {t.hover}; }}
QListWidget#facetList::item:selected {{ background: {t.selected}; color: {t.text}; }}

/* ---------------------------------------------------------------- document list */
QWidget#listHeader {{ background: transparent; }}
QLabel#listTitle {{ color: {t.text}; font-size: 15pt; font-weight: 600; }}
QLabel#listSubtitle {{ color: {t.text_secondary}; }}
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
    padding: 6px 6px; font-weight: 600;
}}
QHeaderView::section:hover {{ background: {t.hover}; }}
QLabel#emptyTitle {{ color: {t.text}; font-size: 13pt; font-weight: 600; }}
QLabel#emptyText {{ color: {t.text_secondary}; }}

/* ---------------------------------------------------------------- inspector */
QTabWidget#inspector::pane {{ border: none; border-top: 1px solid {t.divider}; background: transparent; top: -1px; }}
QTabWidget#inspector > QTabBar {{ background: transparent; }}
QTabWidget#inspector > QTabBar::tab {{
    background: transparent; color: {t.text_secondary}; border: none; border-bottom: 2px solid transparent;
    padding: 9px 4px 7px 4px; margin: 0 8px; font-weight: 600;
}}
QTabWidget#inspector > QTabBar::tab:hover {{ color: {t.text}; }}
QTabWidget#inspector > QTabBar::tab:selected {{ color: {t.text}; border-bottom: 2px solid {t.accent}; }}
QScrollArea#detailsScroll, QWidget#detailsBody, QStackedWidget#details {{ background: transparent; border: none; }}
QLabel#fieldLabel {{ color: {t.text_secondary}; }}
QLabel#sectionLabel {{ color: {t.text}; font-weight: 600; padding-top: 10px; }}
QLabel#multiSelection {{ color: {t.text_secondary}; font-size: 11pt; }}
QLabel#chip {{ background: {t.chip}; color: {t.text}; border-radius: 9px; padding: 2px 10px; min-height: 16px; }}
QLabel#linkLabel {{ color: {t.accent}; }}
QLineEdit[inline="true"], QTextEdit[inline="true"] {{
    background: transparent; color: {t.text}; border: 1px solid transparent; border-radius: 4px; padding: 2px 3px;
    selection-background-color: {t.accent}; selection-color: {t.accent_text};
}}
QLineEdit[inline="true"]:hover, QTextEdit[inline="true"]:hover {{ background: {t.hover}; }}
QLineEdit[inline="true"]:focus, QTextEdit[inline="true"]:focus {{
    background: {t.control}; border: 1px solid {t.control_border}; border-bottom: 2px solid {t.accent};
}}
QTextEdit#title {{ font-size: 15pt; font-weight: 600; }}
QTextEdit#authors {{ font-size: 11pt; color: {t.text_secondary}; }}
QLineEdit#journal {{ font-style: italic; }}
QFrame#reviewBanner {{ background: {t.warning_bg}; border: 1px solid {t.warning_border}; border-radius: 6px; }}
QFrame#reviewBanner QLabel {{ color: {t.text}; background: transparent; }}
QTextEdit#notes {{ background: transparent; color: {t.text}; border: none; padding: 8px; }}
QTreeWidget#outline {{ background: transparent; color: {t.text}; border: none; }}
QTreeWidget#outline::item {{ padding: 3px 0; border-radius: 4px; }}
QTreeWidget#outline::item:hover {{ background: {t.hover}; }}
QTreeWidget#outline::item:selected {{ background: {t.selected}; color: {t.text}; }}

/* ---------------------------------------------------------------- controls */
QPushButton {{
    background: {t.control}; color: {t.text}; border: 1px solid {t.control_border};
    border-bottom-color: {t.control_bottom}; border-radius: 6px; padding: 5px 14px; min-height: 18px;
}}
QPushButton:hover {{ background: {t.control_hover}; }}
QPushButton:pressed {{ background: {t.hover}; color: {t.text_secondary}; }}
QPushButton:disabled {{ color: {t.text_tertiary}; }}
QPushButton#primary {{
    background: {t.accent}; color: {t.accent_text}; border: 1px solid {t.accent}; font-weight: 600;
}}
QPushButton#primary:hover {{ background: {t.accent_hover}; }}
QPushButton#subtle {{ background: transparent; border: 1px solid transparent; text-align: left; padding: 5px 8px; }}
QPushButton#subtle:hover {{ background: {t.hover}; }}
QComboBox {{
    background: {t.control}; color: {t.text}; border: 1px solid {t.control_border};
    border-bottom-color: {t.control_bottom}; border-radius: 6px; padding: 4px 10px; min-height: 18px;
}}
QComboBox:hover {{ background: {t.control_hover}; }}
QComboBox::drop-down {{ border: none; width: 22px; }}
QComboBox::down-arrow {{ image: url("{chevron}"); width: 12px; height: 12px; }}
QComboBox QAbstractItemView {{
    background: {t.card}; color: {t.text}; border: 1px solid {t.card_border}; border-radius: 6px;
    selection-background-color: {t.selected}; selection-color: {t.text}; padding: 4px; outline: 0;
}}
QLineEdit {{
    background: {t.control}; color: {t.text}; border: 1px solid {t.control_border};
    border-bottom-color: {t.control_bottom}; border-radius: 6px; padding: 4px 8px; min-height: 18px;
    selection-background-color: {t.accent}; selection-color: {t.accent_text};
}}
QLineEdit:focus {{ border-bottom: 2px solid {t.accent}; padding-bottom: 3px; }}
QGroupBox {{ border: 1px solid {t.card_border}; border-radius: 8px; margin-top: 16px; padding: 12px 10px 10px 10px; background: {t.card}; }}
QGroupBox::title {{ subcontrol-origin: margin; left: 10px; padding: 0 4px; color: {t.text}; font-weight: 600; }}
QCheckBox, QRadioButton, QLabel {{ color: {t.text}; }}
QProgressBar {{ background: {t.divider}; border: none; border-radius: 2px; max-height: 4px; }}
QProgressBar::chunk {{ background: {t.accent}; border-radius: 2px; }}

/* ---------------------------------------------------------------- scroll bars (thin, Fluent-like) */
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 4px 2px 4px 0; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 0 4px 2px 4px; }}
QScrollBar::handle {{ background: {t.scroll}; border-radius: 3px; }}
QScrollBar::handle:vertical {{ min-height: 32px; margin: 0 2px; }}
QScrollBar::handle:horizontal {{ min-width: 32px; margin: 2px 0; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}

/* ---------------------------------------------------------------- status bar */
QStatusBar {{ background: {t.window}; color: {t.text_secondary}; border: none; }}
QStatusBar::item {{ border: none; }}
QStatusBar QLabel {{ color: {t.text_secondary}; padding: 0 6px; }}
QToolButton#statusButton {{ background: transparent; border: 1px solid transparent; border-radius: 4px; padding: 2px; }}
QToolButton#statusButton:hover {{ background: {t.hover}; }}
QToolButton#statusButton:checked {{ background: {t.selected}; }}
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
        QPalette.ColorRole.Link: t.accent,
        QPalette.ColorRole.Light: t.control_hover,
        QPalette.ColorRole.Midlight: t.control_border,
        QPalette.ColorRole.Mid: t.divider,
        QPalette.ColorRole.Dark: t.control_bottom,
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
