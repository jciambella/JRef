"""Deprecated: the look & feel now lives in :mod:`bibmanager.gui.theme` (kept so old imports keep working)."""

from __future__ import annotations

from PySide6.QtWidgets import QApplication

from . import theme


def apply_style(app: QApplication) -> None:
    theme.apply(app, "system")
