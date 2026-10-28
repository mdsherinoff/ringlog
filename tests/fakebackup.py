"""Build small fake iPhone backups (plain and encrypted) that mirror Apple's layout."""

from __future__ import annotations

import hashlib
import os
import plistlib
import sqlite3
import struct
from datetime import datetime, timezone
from pathlib import Path

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.keywrap import aes_key_wrap

from ringlog.backup import file_id
from ringlog.calls import ADDRESS_BOOK, CALL_HISTORY

APPLE_EPOCH = datetime(2001, 1, 1, tzinfo=timezone.utc)

# (utc time, number, originated, answered, duration, call type, provider)
CALLS = [
    (datetime(2026, 10, 1, 9, 15, tzinfo=timezone.utc), "+44 7700 900123", 1, 1, 125.4, 1, "com.apple.Telephony"),
    (datetime(2026, 10, 1, 18, 2, tzinfo=timezone.utc), "07700900123", 0, 1, 3720, 1, "com.apple.Telephony"),
    (datetime(2026, 10, 2, 7, 30, tzinfo=timezone.utc), "+1 555 0100 222", 0, 0, 0, 1, "com.apple.Telephony"),
    (datetime(2026, 10, 3, 12, 0, tzinfo=timezone.utc), "someone@example.com", 1, 1, 600, 16, "com.apple.FaceTime"),
    (datetime(2026, 10, 4, 20, 45, tzinfo=timezone.utc), "+974 5555 1234", 0, 1, 42, 1, "net.whatsapp.WhatsApp"),
]
CONTACTS = [("Alex", "Morgan", None, "+44 (7700) 900-123"), (None, None, "Corner Shop", "+974 5555 1234")]


def _call_db(path: Path):
    db = sqlite3.connect(path)
    db.execute(
        """CREATE TABLE ZCALLRECORD (Z_PK INTEGER PRIMARY KEY, ZANSWERED INTEGER, ZCALLTYPE INTEGER,
           ZORIGINATED INTEGER, ZDATE TIMESTAMP, ZDURATION FLOAT, ZADDRESS VARCHAR, ZNAME VARCHAR,
           ZSERVICE_PROVIDER VARCHAR, ZISO_COUNTRY_CODE VARCHAR)"""
    )
    for when, number, orig, ans, dur, ctype, prov in CALLS:
        db.execute(
            "INSERT INTO ZCALLRECORD (ZANSWERED, ZCALLTYPE, ZORIGINATED, ZDATE, ZDURATION, ZADDRESS, ZSERVICE_PROVIDER, ZISO_COUNTRY_CODE) VALUES (?,?,?,?,?,?,?,?)",
            (ans, ctype, orig, (when - APPLE_EPOCH).total_seconds(), dur, number, prov, "gb"),
        )
    db.commit()
    db.close()


def _address_book(path: Path):
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE ABPerson (ROWID INTEGER PRIMARY KEY, First TEXT, Last TEXT, Organization TEXT)")
    db.execute("CREATE TABLE ABMultiValue (UID INTEGER PRIMARY KEY, record_id INTEGER, property INTEGER, value TEXT)")
    for i, (first, last, org, number) in enumerate(CONTACTS, start=1):
        db.execute("INSERT INTO ABPerson VALUES (?,?,?,?)", (i, first, last, org))
        db.execute("INSERT INTO ABMultiValue (record_id, property, value) VALUES (?,3,?)", (i, number))
        db.execute("INSERT INTO ABMultiValue (record_id, property, value) VALUES (?,4,?)", (i, "x@example.com"))
    db.commit()
    db.close()


def _tlv(tag: bytes, value) -> bytes:
    if isinstance(value, int):
        value = struct.pack(">L", value)
    return tag + struct.pack(">L", len(value)) + value


def _encrypt(key: bytes, data: bytes) -> bytes:
    p = padding.PKCS7(128).padder()
    data = p.update(data) + p.finalize()
    e = Cipher(algorithms.AES(key), modes.CBC(b"\0" * 16)).encryptor()
    return e.update(data) + e.finalize()


def make_backup(root: Path, password: str | None = None) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    work = root / "_src"
    work.mkdir()
    _call_db(work / "calls.db")
    _address_book(work / "ab.db")
    files = {CALL_HISTORY: work / "calls.db", ADDRESS_BOOK: work / "ab.db"}

    manifest = {"IsEncrypted": bool(password), "Version": "10.0", "Lockdown": {"DeviceName": "Test iPhone"}}
    class_keys = {}
    if password:
        salt, dpsl = os.urandom(20), os.urandom(20)
        key = hashlib.pbkdf2_hmac("sha256", password.encode(), dpsl, 1000, 32)
        key = hashlib.pbkdf2_hmac("sha1", key, salt, 100, 32)
        blob = _tlv(b"VERS", 5) + _tlv(b"TYPE", 1) + _tlv(b"UUID", os.urandom(16)) + _tlv(b"HMCK", os.urandom(40))
        blob += _tlv(b"WRAP", 0) + _tlv(b"SALT", salt) + _tlv(b"ITER", 100)
        blob += _tlv(b"DPWT", 1) + _tlv(b"DPIC", 1000) + _tlv(b"DPSL", dpsl)
        for clas in range(1, 12):
            k = os.urandom(32)
            class_keys[clas] = k
            blob += _tlv(b"UUID", os.urandom(16)) + _tlv(b"CLAS", clas) + _tlv(b"WRAP", 3)
            blob += _tlv(b"KTYP", 0) + _tlv(b"WPKY", aes_key_wrap(key, k))
        manifest["BackupKeyBag"] = blob
        mkey = os.urandom(32)
        manifest["ManifestKey"] = struct.pack("<l", 4) + aes_key_wrap(class_keys[4], mkey)

    mdb_path = work / "Manifest.db"
    mdb = sqlite3.connect(mdb_path)
    mdb.execute("CREATE TABLE Files (fileID TEXT PRIMARY KEY, domain TEXT, relativePath TEXT, flags INTEGER, file BLOB)")
    for (domain, rel), src in files.items():
        fid = file_id(domain, rel)
        data = src.read_bytes()
        mbfile = {"$class": plistlib.UID(3), "RelativePath": plistlib.UID(2), "Size": len(data), "ProtectionClass": 3}
        objects = ["$null", mbfile, rel, {"$classname": "MBFile", "$classes": ["MBFile", "NSObject"]}]
        if password:
            fkey = os.urandom(32)
            objects.append({"NS.data": struct.pack("<l", 3) + aes_key_wrap(class_keys[3], fkey)})
            mbfile["EncryptionKey"] = plistlib.UID(4)
            data = _encrypt(fkey, data)
        meta = plistlib.dumps(
            {"$version": 100000, "$archiver": "NSKeyedArchiver", "$top": {"root": plistlib.UID(1)}, "$objects": objects},
            fmt=plistlib.FMT_BINARY,
        )
        (root / fid[:2]).mkdir(exist_ok=True)
        (root / fid[:2] / fid).write_bytes(data)
        mdb.execute("INSERT INTO Files VALUES (?,?,?,1,?)", (fid, domain, rel, meta))
    mdb.commit()
    mdb.close()

    raw = mdb_path.read_bytes()
    (root / "Manifest.db").write_bytes(_encrypt(mkey, raw) if password else raw)
    with open(root / "Manifest.plist", "wb") as f:
        plistlib.dump(manifest, f, fmt=plistlib.FMT_BINARY)
    with open(root / "Info.plist", "wb") as f:
        plistlib.dump(
            {"Device Name": "Test iPhone", "Product Type": "iPhone16,1", "Product Version": "26.1",
             "Last Backup Date": datetime(2026, 10, 5, 8, 0)},
            f,
        )
    for p in work.iterdir():
        p.unlink()
    work.rmdir()
    return root
