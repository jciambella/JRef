"""The few things that differ between Android and a desktop run of the mobile app.

Everything Android-specific (permissions, intents, content URIs) is in this module and goes through
``pyjnius``; on a PC the same functions fall back to the obvious desktop behaviour, so the whole app can be
run and tested with ``python main.py`` before an APK is built.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
import webbrowser
from collections.abc import Callable
from pathlib import Path
from typing import Any

from kivy.utils import platform as kivy_platform

log = logging.getLogger(__name__)

ANDROID = kivy_platform == "android"
PDF_MIME = "application/pdf"


def _activity() -> Any:
    from android import mActivity  # type: ignore[import-not-found]

    return mActivity


def _autoclass(name: str) -> Any:
    from jnius import autoclass  # type: ignore[import-not-found]

    return autoclass(name)


def sdk_version() -> int:
    return int(_autoclass("android.os.Build$VERSION").SDK_INT) if ANDROID else 0


def device_label() -> str:
    if ANDROID:
        try:
            build = _autoclass("android.os.Build")
            return f"{build.MANUFACTURER} {build.MODEL}".strip().title()
        except Exception:  # pragma: no cover - device only
            return "Android device"
    import socket

    return socket.gethostname() or "PC"


# --------------------------------------------------------------------------- storage
def storage_root() -> str:
    """Top of the user-visible storage: where sync apps keep their folders."""
    if ANDROID:
        try:
            return str(_autoclass("android.os.Environment").getExternalStorageDirectory().getAbsolutePath())
        except Exception:  # pragma: no cover - device only
            return "/storage/emulated/0"
    return str(Path.home())


def download_dir() -> str:
    candidate = Path(storage_root()) / ("Download" if ANDROID else "Downloads")
    return str(candidate if candidate.is_dir() else Path(storage_root()))


def cache_dir(app_dir: str) -> Path:
    """Private folder for PDFs received from other apps before they are imported."""
    if ANDROID:
        try:
            return Path(str(_activity().getCacheDir().getAbsolutePath())) / "incoming"
        except Exception:  # pragma: no cover - device only
            pass
    return Path(app_dir) / "incoming"


def has_storage_access() -> bool:
    """Whether the app may read and write the synchronised folder (ordinary paths under shared storage)."""
    if not ANDROID:
        return True
    try:
        if sdk_version() >= 30:
            return bool(_autoclass("android.os.Environment").isExternalStorageManager())
        from android.permissions import Permission, check_permission  # type: ignore[import-not-found]

        return bool(check_permission(Permission.WRITE_EXTERNAL_STORAGE))
    except Exception:  # pragma: no cover - device only
        log.exception("Could not query the storage permission")
        return False


def request_storage_access() -> None:
    """Android 11+: open the system page "All files access" for this app; older versions: the classic prompt.

    The library lives in a folder that another app (the sync client) owns, and SQLite, PDFs and journals are
    ordinary files there, so the narrower per-app storage is not enough. JRef is installed from an APK, not
    from Google Play, where this permission would need a justification.
    """
    if not ANDROID:
        return
    try:
        if sdk_version() >= 30:
            intent_class = _autoclass("android.content.Intent")
            settings = _autoclass("android.provider.Settings")
            uri = _autoclass("android.net.Uri")
            activity = _activity()
            try:
                intent = intent_class(settings.ACTION_MANAGE_APP_ALL_FILES_ACCESS_PERMISSION)
                intent.setData(uri.parse("package:" + activity.getPackageName()))
                activity.startActivity(intent)
            except Exception:
                activity.startActivity(intent_class(settings.ACTION_MANAGE_ALL_FILES_ACCESS_PERMISSION))
        else:
            from android.permissions import Permission, request_permissions  # type: ignore[import-not-found]

            request_permissions([Permission.READ_EXTERNAL_STORAGE, Permission.WRITE_EXTERNAL_STORAGE])
    except Exception:  # pragma: no cover - device only
        log.exception("Could not request the storage permission")


# --------------------------------------------------------------------------- opening and sharing
def _content_uri(path: str) -> Any | None:
    """``content://media/...`` URI of a file in shared storage (what other apps are allowed to read)."""
    resolver = _activity().getContentResolver()
    files = _autoclass("android.provider.MediaStore$Files").getContentUri("external")
    content_uris = _autoclass("android.content.ContentUris")
    for attempt in range(3):
        cursor = resolver.query(files, ["_id"], "_data=?", [path], None)
        try:
            if cursor is not None and cursor.moveToFirst():
                return content_uris.withAppendedId(files, cursor.getLong(0))
        finally:
            if cursor is not None:
                cursor.close()
        if attempt == 0:  # not indexed yet (just delivered by the sync app): ask for a scan and look again
            _autoclass("android.media.MediaScannerConnection").scanFile(_activity(), [path], None, None)
        time.sleep(0.4)
    return None


def open_file(path: str | Path, mime: str = PDF_MIME) -> bool:
    """Open a file with the application the user has chosen for its type. False if nothing could open it."""
    path = str(path)
    if not ANDROID:
        try:
            if sys.platform == "win32":
                os.startfile(path)  # type: ignore[attr-defined]
            else:
                subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", path])
            return True
        except OSError:
            return False
    intent_class = _autoclass("android.content.Intent")
    try:
        uri = _content_uri(path)
    except Exception:
        log.exception("MediaStore lookup failed for %s", path)
        uri = None
    try:
        if uri is None:  # last resort: a file:// URI (viewers with their own storage access can read it)
            _autoclass("android.os.StrictMode").disableDeathOnFileUriExposure()
            uri = _autoclass("android.net.Uri").fromFile(_autoclass("java.io.File")(path))
        intent = intent_class(intent_class.ACTION_VIEW)
        intent.setDataAndType(uri, mime)
        intent.addFlags(intent_class.FLAG_GRANT_READ_URI_PERMISSION | intent_class.FLAG_ACTIVITY_NEW_TASK)
        _activity().startActivity(intent)
        return True
    except Exception:
        log.exception("No application could open %s", path)
        return False


def open_url(url: str) -> bool:
    if not ANDROID:
        return bool(webbrowser.open(url))
    try:
        intent_class = _autoclass("android.content.Intent")
        intent = intent_class(intent_class.ACTION_VIEW, _autoclass("android.net.Uri").parse(url))
        intent.addFlags(intent_class.FLAG_ACTIVITY_NEW_TASK)
        _activity().startActivity(intent)
        return True
    except Exception:
        log.exception("Could not open %s", url)
        return False


def share_text(text: str, subject: str = "") -> bool:
    """Android share sheet for a citation / BibTeX entry. On a PC the caller copies it instead."""
    if not ANDROID:
        return False
    try:
        intent_class = _autoclass("android.content.Intent")
        string = _autoclass("java.lang.String")
        intent = intent_class(intent_class.ACTION_SEND)
        intent.setType("text/plain")
        intent.putExtra(intent_class.EXTRA_TEXT, string(text))
        if subject:
            intent.putExtra(intent_class.EXTRA_SUBJECT, string(subject))
        chooser = intent_class.createChooser(intent, string("Share"))
        chooser.addFlags(intent_class.FLAG_ACTIVITY_NEW_TASK)
        _activity().startActivity(chooser)
        return True
    except Exception:
        log.exception("Could not share the text")
        return False


# --------------------------------------------------------------------------- PDFs sent by other apps
def _display_name(resolver: Any, uri: Any) -> str:
    try:
        cursor = resolver.query(uri, None, None, None, None)
        try:
            if cursor is not None and cursor.moveToFirst():
                index = cursor.getColumnIndex("_display_name")
                if index >= 0:
                    return str(cursor.getString(index) or "")
        finally:
            if cursor is not None:
                cursor.close()
    except Exception:
        pass
    return ""


def _copy_uri(uri: Any, folder: Path) -> Path | None:
    """Copy what a ``content://`` (or ``file://``) URI points to into ``folder``; returns the copy."""
    activity = _activity()
    resolver = activity.getContentResolver()
    name = _display_name(resolver, uri) or str(uri.getLastPathSegment() or "shared.pdf").split("/")[-1]
    if not name.lower().endswith(".pdf"):
        name += ".pdf"
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"{int(time.time() * 1000)}-{name}"
    stream = resolver.openInputStream(uri)
    output = _autoclass("java.io.FileOutputStream")(str(target))
    try:
        _autoclass("android.os.FileUtils").copy(stream, output)  # API 29+
    finally:
        output.close()
        stream.close()
    return target


def incoming_pdfs(folder: Path, intent: Any | None = None) -> list[Path]:
    """PDFs handed to the app ("Share" / "Open with" in a browser or file manager), copied to ``folder``.

    The intent is marked as handled so that rotating the screen or resuming does not import them again.
    """
    if not ANDROID:
        return []
    try:
        intent_class = _autoclass("android.content.Intent")
        intent = intent if intent is not None else _activity().getIntent()
        if intent is None or intent.getBooleanExtra("jref_handled", False):
            return []
        action = str(intent.getAction() or "")
        uris: list[Any] = []
        if action == intent_class.ACTION_VIEW and intent.getData() is not None:
            uris.append(intent.getData())
        elif action == intent_class.ACTION_SEND:
            extra = intent.getParcelableExtra(intent_class.EXTRA_STREAM)
            if extra is not None:
                uris.append(extra)
        elif action == intent_class.ACTION_SEND_MULTIPLE:
            extras = intent.getParcelableArrayListExtra(intent_class.EXTRA_STREAM)
            if extras is not None:
                uris.extend(extras.get(i) for i in range(extras.size()))
        if not uris:
            return []
        intent.putExtra("jref_handled", True)
        from jnius import cast  # type: ignore[import-not-found]

        copies = []
        for uri in uris:
            try:
                copy = _copy_uri(cast("android.net.Uri", uri), folder)
            except Exception:
                log.exception("Could not read a shared file")
                copy = None
            if copy is not None:
                copies.append(copy)
        return copies
    except Exception:  # pragma: no cover - device only
        log.exception("Could not read the incoming intent")
        return []


def on_new_intent(callback: Callable[[Any], None]) -> None:
    """Call ``callback(intent)`` when another app sends a PDF while JRef is already running."""
    if not ANDROID:
        return
    try:
        from android import activity  # type: ignore[import-not-found]

        activity.bind(on_new_intent=callback)
    except Exception:  # pragma: no cover - device only
        log.exception("Could not listen for new intents")
