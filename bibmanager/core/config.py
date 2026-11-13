"""Application configuration.

Two layers, merged key by key:

1. the bundled defaults, ``config.json`` next to the ``bibmanager`` package (protected words, timeouts);
2. one overlay, the first that exists of: an explicit path, ``$BIBMANAGER_CONFIG``, the per-user file
   written by the Preferences dialog (``%APPDATA%\\JRef\\config.json`` on Windows,
   ``~/.config/JRef/config.json`` elsewhere), ``./config.json``.

Settings written by earlier releases (folders ``JBibManager`` 0.5.x, ``BibManager`` <= 0.4) are copied over once by
``migrate_legacy_user_config``.

The per-user file only stores what the user changed (library folder, contact e-mail, import mode,
additional protected words, inbox watcher), so updated defaults in new releases still reach existing users.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import sys
import tempfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .. import APP_NAME, LEGACY_APP_NAMES

log = logging.getLogger(__name__)

CONFIG_FILENAME = "config.json"
CONFIG_ENV_VAR = "BIBMANAGER_CONFIG"
DATA_ENV_VAR = "JREF_DATA_DIR"
REPO_CONFIG_PATH = Path(__file__).resolve().parents[2] / CONFIG_FILENAME
DEFAULT_LIBRARY_ROOT = "~/JRefLibrary"
DEFAULT_CONTACT_EMAIL = "user@example.com"
IMPORT_MODES = ("move", "copy")


def _user_config_base() -> Path:
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support"
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")


def user_config_dir() -> Path:
    return _user_config_base() / APP_NAME


def legacy_user_config_dirs() -> list[Path]:
    """Settings folders of earlier product names, newest first."""
    return [_user_config_base() / name for name in LEGACY_APP_NAMES]


def legacy_user_config_dir() -> Path:
    return legacy_user_config_dirs()[-1]


def user_data_dir() -> Path:
    """Per-device data that must *not* live in a synced folder (device id, local database of a shared library)."""
    if os.environ.get(DATA_ENV_VAR):
        return Path(os.environ[DATA_ENV_VAR])
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / APP_NAME
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME / "data"
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / APP_NAME


def user_config_path() -> Path:
    return user_config_dir() / CONFIG_FILENAME


def migrate_legacy_user_config() -> Path | None:
    """Copy the per-user settings of an earlier product name (JBibManager, else BibManager) to the JRef
    folder, once.

    The old file is left in place (an older version can still be started). Returns the new path if copied.
    """
    target = user_config_path()
    if target.exists():
        return None
    legacy = next((d / CONFIG_FILENAME for d in legacy_user_config_dirs() if (d / CONFIG_FILENAME).is_file()), None)
    if legacy is None:
        return None
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(legacy, target)
    except OSError as exc:  # settings are a convenience: never block start-up
        log.warning("Could not migrate %s: %s", legacy, exc)
        return None
    log.info("Migrated settings %s -> %s", legacy, target)
    return target


@dataclass(slots=True)
class AppConfig:
    library_root: Path
    protected_words: tuple[str, ...] = ()
    import_mode: str = "move"
    contact_email: str = DEFAULT_CONTACT_EMAIL
    http_timeout_s: float = 15.0
    openalex_min_title_similarity: float = 0.85
    write_master_bib: bool = True
    include_abstract_in_bib: bool = False
    extra_protected_words: tuple[str, ...] = ()  # user additions (Preferences), merged into protected_words
    inbox_enabled: bool = True  # Milestone 4: import PDFs saved into the inbox folder automatically
    inbox_folder: Path | None = None  # None -> <library_root>/inbox
    config_path: Path | None = None
    # ---- sharing the library with other devices through a synced folder (see core/sync.py)
    sync_enabled: bool = False  # per-device journals in <library_root>/sync, database kept outside the folder
    device_name: str = ""  # shown to the other devices; "" -> host name
    local_data_dir: Path | None = None  # None -> user_data_dir()
    write_bib_files: bool = True  # False on devices that never run LaTeX (Android): no bibtex/*.bib writes
    adopt_folder_database: bool = True  # sharing an existing library: move <root>/library.db to the data folder
    # Linked PDFs recorded with another device's absolute path: {"C:/Users/x/Papers": "/storage/.../Papers"}
    linked_path_map: dict[str, str] = field(default_factory=dict)
    # ---- Ask AI panel (core/assistant.py). API keys are never stored here (core/secrets.py).
    assistant_provider: str = "anthropic"
    assistant_models: dict[str, str] = field(default_factory=dict)  # provider id -> model
    assistant_base_urls: dict[str, str] = field(default_factory=dict)  # provider id -> server address
    assistant_workspace_id: str = ""  # Claude keys that are not scoped to one workspace
    assistant_max_chars: int = 300_000
    assistant_max_tokens: int = 4096
    assistant_send_pdf: bool = False  # send the PDF file itself (figures, equations) where the provider reads PDFs
    assistant_supplements: bool = True  # include the text of supplementary PDFs
    assistant_acknowledged: tuple[str, ...] = ()  # providers whose data notice the user has accepted

    def __post_init__(self) -> None:
        self.library_root = Path(self.library_root).expanduser()
        if self.local_data_dir is not None and str(self.local_data_dir).strip():
            self.local_data_dir = Path(self.local_data_dir).expanduser()
        else:
            self.local_data_dir = None
        self.linked_path_map = {str(k): str(v) for k, v in dict(self.linked_path_map or {}).items() if k and v}
        self.assistant_models = {str(k): str(v) for k, v in dict(self.assistant_models or {}).items()}
        self.assistant_base_urls = {str(k): str(v) for k, v in dict(self.assistant_base_urls or {}).items() if v}
        self.assistant_acknowledged = tuple(dict.fromkeys(str(p) for p in self.assistant_acknowledged or ()))
        self.assistant_max_chars = max(20_000, int(self.assistant_max_chars))
        self.assistant_max_tokens = max(256, int(self.assistant_max_tokens))
        if self.inbox_folder is not None and str(self.inbox_folder).strip():
            self.inbox_folder = Path(self.inbox_folder).expanduser()
        else:
            self.inbox_folder = None
        if self.import_mode not in IMPORT_MODES:
            raise ValueError(f"import_mode must be one of {IMPORT_MODES}, got {self.import_mode!r}")
        if not 0.0 < self.openalex_min_title_similarity <= 1.0:
            raise ValueError("openalex_min_title_similarity must lie in (0, 1]")
        self.extra_protected_words = _dedupe(self.extra_protected_words)
        self.protected_words = _dedupe((*self.protected_words, *self.extra_protected_words))

    @property
    def inbox_path(self) -> Path:
        return self.inbox_folder if self.inbox_folder is not None else self.library_root / "inbox"

    @property
    def data_dir(self) -> Path:
        return self.local_data_dir if self.local_data_dir is not None else user_data_dir()

    @property
    def user_agent(self) -> str:
        """Polite-pool User-Agent required by Crossref/OpenAlex etiquette."""
        return f"{APP_NAME}/1.0 (mailto:{self.contact_email or DEFAULT_CONTACT_EMAIL})"

    @classmethod
    def from_dict(cls, data: Mapping[str, Any], *, base_dir: Path | None = None) -> AppConfig:
        network = data.get("network") or {}
        bibtex = data.get("bibtex") or {}
        inbox = data.get("inbox") or {}
        sync = data.get("sync") or {}
        assistant = data.get("assistant") or {}
        root = Path(str(data.get("library_root", DEFAULT_LIBRARY_ROOT))).expanduser()
        if not root.is_absolute() and base_dir is not None:
            root = base_dir / root
        return cls(
            library_root=root,
            protected_words=tuple(data.get("protected_words") or ()),
            import_mode=str(data.get("import_mode", "move")),
            contact_email=str(network.get("contact_email", DEFAULT_CONTACT_EMAIL)),
            http_timeout_s=float(network.get("timeout_s", 15.0)),
            openalex_min_title_similarity=float(network.get("openalex_min_title_similarity", 0.85)),
            write_master_bib=bool(bibtex.get("write_master", True)),
            include_abstract_in_bib=bool(bibtex.get("include_abstract", False)),
            extra_protected_words=tuple(data.get("extra_protected_words") or ()),
            inbox_enabled=bool(inbox.get("enabled", True)),
            inbox_folder=Path(str(inbox["folder"])) if inbox.get("folder") else None,
            sync_enabled=bool(sync.get("enabled", False)),
            device_name=str(sync.get("device_name") or ""),
            local_data_dir=Path(str(sync["local_data_dir"])) if sync.get("local_data_dir") else None,
            write_bib_files=bool(bibtex.get("write_files", True)),
            linked_path_map=dict(data.get("linked_path_map") or {}),
            assistant_provider=str(assistant.get("provider") or "anthropic"),
            assistant_models=dict(assistant.get("models") or {}),
            assistant_base_urls=dict(assistant.get("base_urls") or {}),
            assistant_workspace_id=str(assistant.get("workspace_id") or ""),
            assistant_max_chars=int(assistant.get("max_chars") or 300_000),
            assistant_max_tokens=int(assistant.get("max_tokens") or 4096),
            assistant_send_pdf=bool(assistant.get("send_pdf", False)),
            assistant_supplements=bool(assistant.get("supplements", True)),
            assistant_acknowledged=tuple(assistant.get("acknowledged") or ()),
        )

    @classmethod
    def load(cls, path: str | Path | None = None, **overrides: Any) -> AppConfig:
        """Bundled defaults + one overlay (explicit path, $BIBMANAGER_CONFIG, user file, ./config.json).

        Keyword ``overrides`` replace individual dataclass fields (e.g. ``library_root=tmp``).
        """
        overlay_path: Path | None
        if path is not None:
            overlay_path = Path(path)
            if not overlay_path.is_file():
                raise FileNotFoundError(path)
        else:
            migrate_legacy_user_config()
            candidates = [Path(os.environ[CONFIG_ENV_VAR])] if os.environ.get(CONFIG_ENV_VAR) else []
            candidates += [user_config_path(), Path.cwd() / CONFIG_FILENAME]
            overlay_path = next((c for c in candidates if c.is_file()), None)

        data = _read_json(REPO_CONFIG_PATH) if REPO_CONFIG_PATH.is_file() else {}
        if overlay_path is not None and overlay_path.resolve() != REPO_CONFIG_PATH:
            data = _deep_merge(data, _read_json(overlay_path))
        anchor = overlay_path or (REPO_CONFIG_PATH if REPO_CONFIG_PATH.is_file() else None)
        config = cls.from_dict(data, base_dir=anchor.parent if anchor else None)
        config.config_path = anchor
        for key, value in overrides.items():
            if not hasattr(config, key):
                raise TypeError(f"Unknown configuration field: {key}")
            setattr(config, key, value)
        config.__post_init__()
        return config

    def user_settings(self) -> dict[str, Any]:
        """The keys the Preferences dialog owns (written to the per-user overlay)."""
        return {
            "library_root": str(self.library_root),
            "import_mode": self.import_mode,
            "network": {"contact_email": self.contact_email},
            "extra_protected_words": list(self.extra_protected_words),
            "inbox": {
                "enabled": self.inbox_enabled,
                "folder": str(self.inbox_folder) if self.inbox_folder is not None else None,
            },
            "sync": {"enabled": self.sync_enabled, "device_name": self.device_name},
            "linked_path_map": dict(self.linked_path_map),
            "assistant": {
                "provider": self.assistant_provider,
                "models": dict(self.assistant_models),
                "base_urls": dict(self.assistant_base_urls),
                "workspace_id": self.assistant_workspace_id,
                "max_chars": self.assistant_max_chars,
                "max_tokens": self.assistant_max_tokens,
                "send_pdf": self.assistant_send_pdf,
                "supplements": self.assistant_supplements,
                "acknowledged": list(self.assistant_acknowledged),
            },
        }

    def save_user_settings(self, path: Path | None = None) -> Path:
        """Merge ``user_settings()`` into the per-user file (atomic write); returns its path."""
        target = Path(path) if path is not None else user_config_path()
        current = _read_json(target) if target.is_file() else {}
        merged = _deep_merge(current, self.user_settings())
        merged["extra_protected_words"] = list(self.extra_protected_words)  # lists are replaced, not merged
        merged["linked_path_map"] = dict(self.linked_path_map)  # so is the path map (entries can be removed)
        merged["assistant"] = self.user_settings()["assistant"]  # and the assistant block (dicts are replaced)
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".config.", suffix=".json", dir=target.parent)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(merged, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
        os.replace(tmp, target)
        return target


def _read_json(path: Path) -> dict[str, Any]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected a JSON object")
    return data


def _deep_merge(base: Mapping[str, Any], overlay: Mapping[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in overlay.items():
        if isinstance(value, Mapping) and isinstance(merged.get(key), Mapping):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _dedupe(words: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(w.strip() for w in words if w and w.strip()))
