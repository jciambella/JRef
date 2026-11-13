"""Storage of API keys (Ask AI panel): never in ``config.json``, never in the synchronised library folder.

* Windows: the key is encrypted with DPAPI (``CryptProtectData``), i.e. with the Windows login of the user, and the
  ciphertext is kept in ``%APPDATA%\\JRef\\secrets.json``. Another Windows account, or a copy of the file on another
  computer, cannot decrypt it. No third-party package is needed (``ctypes``).
* Elsewhere: the system keyring through the ``keyring`` package when it is installed and has a working back end;
  otherwise ``secrets.json`` readable by the user only (permissions 0600).
* Environment variables (``ANTHROPIC_API_KEY``, ``OPENAI_API_KEY``, ``GEMINI_API_KEY`` / ``GOOGLE_API_KEY``) are used
  when nothing is stored, so keys already configured for other tools work.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import sys
import tempfile
from pathlib import Path

from .. import APP_NAME

log = logging.getLogger(__name__)

SECRETS_FILE = "secrets.json"
ENV_VARS = {
    "anthropic": ("ANTHROPIC_API_KEY",),
    "openai": ("OPENAI_API_KEY",),
    "gemini": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
}


# --------------------------------------------------------------------------- Windows DPAPI
def _dpapi(data: bytes, *, protect: bool) -> bytes:
    import ctypes
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    crypt32 = ctypes.windll.crypt32  # type: ignore[attr-defined]
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    buffer = ctypes.create_string_buffer(data, len(data))
    source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))
    entropy_bytes = f"{APP_NAME} API key".encode()
    entropy_buffer = ctypes.create_string_buffer(entropy_bytes, len(entropy_bytes))
    entropy = Blob(len(entropy_bytes), ctypes.cast(entropy_buffer, ctypes.POINTER(ctypes.c_char)))
    result = Blob()
    flags = 0x1  # CRYPTPROTECT_UI_FORBIDDEN
    call = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    ok = call(ctypes.byref(source), None, ctypes.byref(entropy), None, None, flags, ctypes.byref(result))
    if not ok:
        raise ctypes.WinError()  # type: ignore[attr-defined]
    try:
        return ctypes.string_at(result.pbData, result.cbData)
    finally:
        kernel32.LocalFree(result.pbData)


# --------------------------------------------------------------------------- the store
class KeyStore:
    """API keys by provider id (``"anthropic"``, ``"openai"``, ``"gemini"``, ``"compatible"``, ...)."""

    def __init__(self, folder: Path, *, use_keyring: bool = True, use_environment: bool = True) -> None:
        self.path = Path(folder) / SECRETS_FILE
        self.use_environment = use_environment
        self._keyring = None
        if use_keyring and sys.platform != "win32":
            try:
                import keyring
                from keyring.backends.fail import Keyring as FailKeyring

                if not isinstance(keyring.get_keyring(), FailKeyring):
                    self._keyring = keyring
            except Exception:  # not installed, or no usable back end (headless Linux)
                self._keyring = None

    @property
    def backend(self) -> str:
        if sys.platform == "win32":
            return "Windows (encrypted with your Windows login)"
        if self._keyring is not None:
            return "system keyring"
        return f"file readable only by you ({self.path})"

    # -- file helpers
    def _read(self) -> dict[str, str]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _write(self, data: dict[str, str]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".secrets.", suffix=".json", dir=self.path.parent)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
        if sys.platform != "win32":
            os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)

    @staticmethod
    def _encode(secret: str) -> str:
        raw = secret.encode("utf-8")
        if sys.platform == "win32":
            return "dpapi:" + base64.b64encode(_dpapi(raw, protect=True)).decode("ascii")
        return "plain:" + base64.b64encode(raw).decode("ascii")

    @staticmethod
    def _decode(stored: str) -> str:
        kind, _, payload = stored.partition(":")
        raw = base64.b64decode(payload)
        if kind == "dpapi":
            return _dpapi(raw, protect=False).decode("utf-8")
        return raw.decode("utf-8")

    # -- API
    def get(self, provider: str) -> str:
        """The stored key, else the provider's environment variable, else ``""``."""
        secret = ""
        if self._keyring is not None:
            try:
                secret = self._keyring.get_password(APP_NAME, provider) or ""
            except Exception as exc:  # pragma: no cover - keyring back end failure
                log.warning("Keyring read failed: %s", exc)
        else:
            stored = self._read().get(provider)
            if stored:
                try:
                    secret = self._decode(stored)
                except Exception as exc:  # e.g. the file was copied from another Windows account
                    log.warning("Stored key for %s cannot be decrypted: %s", provider, exc)
        if not secret and self.use_environment:
            secret = next((os.environ[v] for v in ENV_VARS.get(provider, ()) if os.environ.get(v)), "")
        return secret.strip()

    def is_stored(self, provider: str) -> bool:
        if self._keyring is not None:
            try:
                return bool(self._keyring.get_password(APP_NAME, provider))
            except Exception:  # pragma: no cover
                return False
        return provider in self._read()

    def set(self, provider: str, secret: str) -> None:
        secret = secret.strip()
        if not secret:
            self.delete(provider)
            return
        if self._keyring is not None:
            self._keyring.set_password(APP_NAME, provider, secret)
            return
        data = self._read()
        data[provider] = self._encode(secret)
        self._write(data)

    def delete(self, provider: str) -> None:
        if self._keyring is not None:
            try:
                self._keyring.delete_password(APP_NAME, provider)
            except Exception:  # not stored
                pass
            return
        data = self._read()
        if data.pop(provider, None) is not None:
            self._write(data)
