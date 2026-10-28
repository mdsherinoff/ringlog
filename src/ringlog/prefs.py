"""Small per-user settings file, plus optional backup passwords in the system keychain.

Settings never include call data or passwords. Passwords are stored only when the
user ticks "Remember", in the macOS Keychain / Windows Credential Manager.
"""

from __future__ import annotations

import json
import os
import sys
import threading
from pathlib import Path

_SERVICE = "Ringlog backup password"
_KEYS = {"last_backup", "export_dir", "window", "view", "sort", "remembered"}
_lock = threading.Lock()


def config_dir() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library/Application Support/Ringlog"
    if sys.platform.startswith("win"):
        return Path(os.environ.get("APPDATA") or Path.home()) / "Ringlog"
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "ringlog"


class Prefs:
    def __init__(self, path: Path | None = None):
        self.path = path or config_dir() / "prefs.json"
        try:
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(self.data, dict):
                self.data = {}
        except (OSError, ValueError):
            self.data = {}

    def get(self, key, default=None):
        return self.data.get(key, default)

    def update(self, values: dict) -> None:
        with _lock:
            self.data.update({k: v for k, v in values.items() if k in _KEYS})
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self.path.with_suffix(".tmp")
                tmp.write_text(json.dumps(self.data, indent=2), encoding="utf-8")
                tmp.replace(self.path)
            except OSError:
                pass  # settings are a convenience; never fail the app over them

    # Which backups have a saved password (ids only, so we don't touch the keychain to list them).
    def remembered(self) -> set[str]:
        return set(self.get("remembered", []))

    def set_remembered(self, backup_id: str, on: bool) -> None:
        ids = self.remembered()
        ids.add(backup_id) if on else ids.discard(backup_id)
        self.update({"remembered": sorted(ids)})


def _keyring():
    try:
        import keyring

        return keyring
    except Exception:
        return None


def keychain_available() -> bool:
    return _keyring() is not None


def load_password(backup_id: str) -> str | None:
    kr = _keyring()
    if not kr:
        return None
    try:
        return kr.get_password(_SERVICE, backup_id)
    except Exception:
        return None


def save_password(backup_id: str, password: str) -> bool:
    kr = _keyring()
    if not kr:
        return False
    try:
        kr.set_password(_SERVICE, backup_id, password)
        return True
    except Exception:
        return False


def forget_password(backup_id: str) -> None:
    kr = _keyring()
    if kr:
        try:
            kr.delete_password(_SERVICE, backup_id)
        except Exception:
            pass
