"""Read files out of a local iPhone backup made by Finder, iTunes or Apple Devices.

A backup folder holds Info.plist, Manifest.plist, Manifest.db (an index of every
file) and the files themselves, stored as <first two hex chars>/<sha1 file id>.
Encrypted backups wrap every file key with class keys that are unlocked by the
backup password; see `_Keybag` for the details.
"""

from __future__ import annotations

import hashlib
import os
import plistlib
import shutil
import sqlite3
import struct
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


class BackupError(Exception):
    """The backup can't be read (missing, damaged, wrong password, ...)."""


class PasswordRequired(BackupError):
    """The backup is encrypted and no password was given."""


class WrongPassword(BackupError):
    """The password didn't unlock the backup."""


def default_backup_dirs() -> list[Path]:
    """Folders where Apple's tools keep backups on this computer."""
    home = Path.home()
    if sys.platform == "darwin":
        return [home / "Library/Application Support/MobileSync/Backup"]
    if sys.platform.startswith("win"):
        dirs = [home / "Apple/MobileSync/Backup"]  # Microsoft Store iTunes / Apple Devices
        appdata = os.environ.get("APPDATA")
        if appdata:
            dirs.append(Path(appdata) / "Apple Computer/MobileSync/Backup")
        return dirs
    return [home / ".local/share/MobileSync/Backup"]


@dataclass
class BackupInfo:
    path: Path
    device_name: str
    product: str
    ios_version: str
    last_backup: datetime | None
    encrypted: bool

    @property
    def label(self) -> str:
        when = self.last_backup.strftime("%Y-%m-%d %H:%M") if self.last_backup else "unknown date"
        lock = ", encrypted" if self.encrypted else ""
        return f"{self.device_name} ({self.product}, iOS {self.ios_version}) - {when}{lock}"


def read_info(path: Path) -> BackupInfo:
    path = Path(path)
    if not (path / "Manifest.plist").is_file():
        raise BackupError(f"{path} doesn't look like an iPhone backup (no Manifest.plist).")
    with open(path / "Manifest.plist", "rb") as f:
        manifest = plistlib.load(f)
    info = {}
    if (path / "Info.plist").is_file():
        with open(path / "Info.plist", "rb") as f:
            info = plistlib.load(f)
    lockdown = manifest.get("Lockdown", {})
    return BackupInfo(
        path=path,
        device_name=info.get("Device Name") or lockdown.get("DeviceName") or path.name,
        product=info.get("Product Type") or lockdown.get("ProductType") or "iPhone",
        ios_version=info.get("Product Version") or lockdown.get("ProductVersion") or "?",
        last_backup=info.get("Last Backup Date") or manifest.get("Date"),
        encrypted=bool(manifest.get("IsEncrypted")),
    )


def find_backups(dirs: list[Path] | None = None) -> list[BackupInfo]:
    """Every readable backup in the default folders, newest first."""
    found = []
    for root in dirs or default_backup_dirs():
        try:
            children = list(Path(root).iterdir())
        except OSError:
            continue
        for child in children:
            try:
                found.append(read_info(child))
            except (BackupError, OSError, plistlib.InvalidFileException):
                continue
    found.sort(key=lambda b: b.last_backup or datetime.min, reverse=True)
    return found


def file_id(domain: str, relative_path: str) -> str:
    return hashlib.sha1(f"{domain}-{relative_path}".encode()).hexdigest()


# --- Encryption --------------------------------------------------------------

_CLASSKEY_TAGS = {b"CLAS", b"WRAP", b"WPKY", b"KTYP", b"PBKY"}
_WRAP_PASSCODE = 2


def _tlv_blocks(blob: bytes):
    i = 0
    while i + 8 <= len(blob):
        tag = blob[i : i + 4]
        length = struct.unpack(">L", blob[i + 4 : i + 8])[0]
        yield tag, blob[i + 8 : i + 8 + length]
        i += 8 + length


class _Keybag:
    """The BackupKeyBag from Manifest.plist: one wrapped AES key per protection class.

    The password is stretched twice (PBKDF2-SHA256 with DPSL/DPIC, then
    PBKDF2-SHA1 with SALT/ITER) into a key that unwraps each class key (RFC 3394).
    """

    def __init__(self, blob: bytes):
        self.uuid = None
        self.wrap = None
        self.attrs: dict[bytes, object] = {}
        self.class_keys: dict[int, dict[bytes, object]] = {}
        current = None
        for tag, value in _tlv_blocks(blob):
            if len(value) == 4:
                value = struct.unpack(">L", value)[0]
            if tag == b"UUID" and self.uuid is None:
                self.uuid = value
            elif tag == b"WRAP" and self.wrap is None:
                self.wrap = value
            elif tag == b"UUID":
                if current:
                    self.class_keys[current[b"CLAS"]] = current
                current = {b"UUID": value}
            elif tag in _CLASSKEY_TAGS and current is not None:
                current[tag] = value
            else:
                self.attrs[tag] = value
        if current:
            self.class_keys[current[b"CLAS"]] = current

    def unlock(self, password: bytes) -> None:
        from cryptography.hazmat.primitives.keywrap import InvalidUnwrap, aes_key_unwrap

        key = password
        if b"DPSL" in self.attrs:
            key = hashlib.pbkdf2_hmac("sha256", key, self.attrs[b"DPSL"], self.attrs[b"DPIC"], 32)
        key = hashlib.pbkdf2_hmac("sha1", key, self.attrs[b"SALT"], self.attrs[b"ITER"], 32)
        for ck in self.class_keys.values():
            if b"WPKY" in ck and ck[b"WRAP"] & _WRAP_PASSCODE:
                try:
                    ck[b"KEY"] = aes_key_unwrap(key, ck[b"WPKY"])
                except InvalidUnwrap:
                    raise WrongPassword("That backup password is not correct.") from None

    def unwrap(self, protection_class: int, wrapped: bytes) -> bytes:
        from cryptography.hazmat.primitives.keywrap import aes_key_unwrap

        ck = self.class_keys.get(protection_class)
        if not ck or b"KEY" not in ck:
            raise BackupError(f"No key for protection class {protection_class}.")
        return aes_key_unwrap(ck[b"KEY"], wrapped)


def _aes_decrypt(key: bytes, data: bytes) -> bytes:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    data = data[: len(data) - len(data) % 16]
    d = Cipher(algorithms.AES(key), modes.CBC(b"\0" * 16)).decryptor()
    return d.update(data) + d.finalize()


def _strip_padding(data: bytes) -> bytes:
    n = data[-1] if data else 0
    if 1 <= n <= 16 and data.endswith(bytes([n]) * n):
        return data[:-n]
    return data


# --- Backup ------------------------------------------------------------------


class Backup:
    """An opened backup. Use as a context manager so decrypted temp files are removed."""

    def __init__(self, path: Path | str, password: str | None = None):
        self.info = read_info(Path(path))
        self.path = self.info.path
        self._tmp = Path(tempfile.mkdtemp(prefix="ringlog-"))
        self._keybag = None
        try:
            self._open_manifest(password)
        except Exception:
            self.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self):
        if getattr(self, "_db", None):
            self._db.close()
            self._db = None
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _open_manifest(self, password: str | None):
        manifest_db = self.path / "Manifest.db"
        if not manifest_db.is_file():
            if (self.path / "Manifest.mbdb").is_file():
                raise BackupError("This backup is from iOS 9 or older, which isn't supported.")
            raise BackupError("Manifest.db is missing; the backup may be incomplete.")
        if self.info.encrypted:
            if not password:
                raise PasswordRequired("This backup is encrypted. Enter the backup password.")
            with open(self.path / "Manifest.plist", "rb") as f:
                manifest = plistlib.load(f)
            self._keybag = _Keybag(manifest["BackupKeyBag"])
            self._keybag.unlock(password.encode())
            mk = manifest["ManifestKey"]
            key = self._keybag.unwrap(struct.unpack("<l", mk[:4])[0], mk[4:])
            plain = _strip_padding(_aes_decrypt(key, manifest_db.read_bytes()))
            if not plain.startswith(b"SQLite format 3"):
                raise WrongPassword("Couldn't decrypt the backup index; the password may be wrong.")
            manifest_db = self._tmp / "Manifest.db"
            manifest_db.write_bytes(plain)
        self._db = sqlite3.connect(f"file:{manifest_db}?mode=ro", uri=True)

    def _row(self, domain: str, relative_path: str):
        return self._db.execute(
            "SELECT fileID, file FROM Files WHERE domain = ? AND relativePath = ?",
            (domain, relative_path),
        ).fetchone()

    def extract(self, domain: str, relative_path: str, dest: Path) -> bool:
        """Copy one file (decrypted if needed) to dest. False when it isn't in the backup."""
        row = self._row(domain, relative_path)
        if not row:
            return False
        fid, meta = row
        src = self.path / fid[:2] / fid
        if not src.is_file():
            src = self.path / fid  # very old flat layout
            if not src.is_file():
                return False
        if not self._keybag:
            shutil.copyfile(src, dest)
            return True
        archive = plistlib.loads(meta)
        objects = archive["$objects"]
        top = objects[archive["$top"]["root"].data]
        key_obj = objects[top["EncryptionKey"].data]
        wrapped = key_obj["NS.data"] if isinstance(key_obj, dict) else key_obj
        key = self._keybag.unwrap(top["ProtectionClass"], wrapped[4:])
        data = _aes_decrypt(key, src.read_bytes())
        size = top.get("Size")
        data = data[:size] if size is not None else _strip_padding(data)
        dest.write_bytes(data)
        return True

    def extract_sqlite(self, domain: str, relative_path: str) -> Path | None:
        """Extract a SQLite database with its -wal/-shm companions; returns the temp path."""
        dest = self._tmp / hashlib.sha1(relative_path.encode()).hexdigest()
        if not self.extract(domain, relative_path, dest):
            return None
        for suffix in ("-wal", "-shm"):
            self.extract(domain, relative_path + suffix, Path(str(dest) + suffix))
        return dest
