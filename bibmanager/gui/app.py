"""Application entry point: ``jref`` / ``python -m bibmanager``."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

from PySide6.QtCore import QSettings, Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QApplication, QMessageBox

from .. import APP_NAME, APP_SUBTITLE, LEGACY_APP_NAMES, __version__
from ..core.config import CONFIG_ENV_VAR, AppConfig, migrate_legacy_user_config, user_config_path
from ..core.library import Library
from . import icons, theme
from .dialogs import PreferencesDialog
from .main_window import MainWindow

log = logging.getLogger(__name__)


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="jref", description=f"{APP_SUBTITLE}: local reference manager for LaTeX writing.")
    parser.add_argument("--config", type=Path, help="path to a config.json overlay (default: per-user settings)")
    parser.add_argument("--library", type=Path, help="library root folder (overrides the configuration)")
    parser.add_argument("--version", action="version", version=f"{APP_NAME} {__version__}")
    return parser.parse_args(argv)


def migrate_legacy_qsettings(target: QSettings | None = None, legacy: QSettings | None = None) -> int:
    """Copy window layout/theme settings of the previous product name (JBibManager 0.5.x, else BibManager
    <= 0.4) into the JRef scope, once."""
    target = target if target is not None else QSettings()
    if target.allKeys():
        return 0
    if legacy is None:
        candidates = [QSettings(name, name) for name in LEGACY_APP_NAMES]
        legacy = next((s for s in candidates if s.allKeys()), candidates[-1])
    keys = legacy.allKeys()
    for key in keys:
        target.setValue(key, legacy.value(key))
    target.sync()
    return len(keys)


def _log_file() -> Path:
    path = user_config_path().parent / "jref.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


class Application:
    """Owns the QApplication, the open Library and its window; can switch libraries at run time."""

    def __init__(self, app: QApplication, config: AppConfig) -> None:
        self.app = app
        self.library: Library | None = None
        self.window: MainWindow | None = None
        self.open(config)

    def open(self, config: AppConfig) -> None:
        library = Library(config)
        window = MainWindow(library)
        window.libraryChangeRequested.connect(self.switch)
        window.show()
        self.library, self.window = library, window

    def switch(self, config: AppConfig) -> None:
        old_window, old_library = self.window, self.library
        try:
            self.open(config)
        except Exception as exc:
            QMessageBox.critical(old_window, APP_NAME, f"Cannot open the library:\n\n{exc}")
            return
        if old_window is not None:
            old_window.close()
            old_window.deleteLater()
        if old_library is not None:
            old_library.close()

    def close(self) -> None:
        if self.library is not None:
            self.library.close()


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv if argv is None else argv)
    args = _parse_args(argv[1:])
    migrate_legacy_user_config()  # before the log file and the first-run check look at the settings folder
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    try:
        handlers.append(logging.FileHandler(_log_file(), encoding="utf-8"))
    except OSError:
        pass
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", handlers=handlers)
    if sys.platform == "win32":  # own taskbar icon/grouping instead of python.exe's
        try:
            import ctypes

            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(f"{APP_NAME}.Desktop")
        except (AttributeError, OSError):
            pass
    QGuiApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication.instance() or QApplication(argv[:1])
    assert isinstance(app, QApplication)
    app.setApplicationName(APP_NAME)
    app.setOrganizationName(APP_NAME)
    app.setApplicationVersion(__version__)
    migrate_legacy_qsettings()
    app.setWindowIcon(icons.app_icon())
    theme.capture_system_accent(app)
    theme.apply(app, str(QSettings().value("view/theme", "system")))

    overrides = {"library_root": args.library.expanduser()} if args.library else {}
    try:
        config = AppConfig.load(args.config, **overrides)
    except Exception as exc:  # show the reason instead of a silent crash on double-click start
        QMessageBox.critical(None, APP_NAME, f"Cannot read the configuration:\n\n{exc}")
        return 1

    first_run = not (args.config or args.library or os.environ.get(CONFIG_ENV_VAR) or user_config_path().exists())
    if first_run:
        welcome = PreferencesDialog(None, config=config, theme_mode="system", first_run=True)
        if welcome.exec() != PreferencesDialog.DialogCode.Accepted:
            return 0
        values = welcome.values()
        config.library_root = values.library_root
        config.contact_email = values.contact_email
        config.import_mode = values.import_mode
        config.save_user_settings()

    try:
        application = Application(app, config)
    except Exception as exc:
        log.exception("Cannot open the library")
        QMessageBox.critical(None, APP_NAME, f"Cannot open the library:\n\n{exc}")
        return 1
    try:
        return app.exec()
    finally:
        application.close()


if __name__ == "__main__":
    raise SystemExit(main())
