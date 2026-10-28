"""Turn the call history database inside a backup into a list of `Call`s."""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .backup import Backup, BackupError

CALL_HISTORY = ("HomeDomain", "Library/CallHistoryDB/CallHistory.storedata")
ADDRESS_BOOK = ("HomeDomain", "Library/AddressBook/AddressBook.sqlitedb")

# Core Data stores dates as seconds since 2001-01-01 UTC.
_APPLE_EPOCH = datetime(2001, 1, 1, tzinfo=timezone.utc)

_CALL_TYPES = {1: "Phone", 8: "FaceTime Video", 16: "FaceTime Audio"}
_PROVIDERS = {
    "com.apple.telephony": "Phone",
    "com.apple.facetime": "FaceTime",
    "net.whatsapp.whatsapp": "WhatsApp",
    "com.skype.skype": "Skype",
    "ph.telegra.telegraph": "Telegram",
    "org.whispersystems.signal": "Signal",
    "com.viber": "Viber",
    "com.microsoft.teams": "Teams",
    "us.zoom.videomeetings": "Zoom",
}

INCOMING, OUTGOING, MISSED = "Incoming", "Outgoing", "Missed"


@dataclass
class Call:
    when: datetime  # local time, timezone-aware
    number: str
    name: str
    direction: str  # INCOMING / OUTGOING / MISSED
    duration: int  # seconds
    service: str
    country: str

    @property
    def contact(self) -> str:
        return self.name or self.number or "Unknown"

    @property
    def duration_text(self) -> str:
        h, rest = divmod(self.duration, 3600)
        m, s = divmod(rest, 60)
        return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def number_key(number: str) -> str:
    """Digits only, trimmed to the last 9 so +44 7700 900123 matches 07700 900123."""
    digits = re.sub(r"\D", "", number or "")
    return digits[-9:] if len(digits) >= 9 else digits


def _columns(db: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in db.execute(f"PRAGMA table_info({table})")}


def load_contacts(path: Path) -> dict[str, str]:
    """Map number_key -> display name from AddressBook.sqlitedb."""
    names: dict[str, str] = {}
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        rows = db.execute(
            """SELECT p.First, p.Last, p.Organization, v.value
               FROM ABMultiValue v JOIN ABPerson p ON p.ROWID = v.record_id
               WHERE v.property = 3"""  # 3 = phone number
        ).fetchall()
    except sqlite3.DatabaseError:
        return names
    finally:
        db.close()
    for first, last, org, value in rows:
        name = " ".join(x for x in (first, last) if x) or org
        key = number_key(value)
        if name and key:
            names.setdefault(key, name)
    return names


def _service(call_type, provider) -> str:
    provider = (provider or "").lower()
    if provider in _PROVIDERS and provider != "com.apple.facetime":
        return _PROVIDERS[provider]
    if call_type in _CALL_TYPES:
        return _CALL_TYPES[call_type]
    if provider:
        return _PROVIDERS.get(provider) or provider.rsplit(".", 1)[-1].title()
    return "Phone"


def read_call_db(path: Path, contacts: dict[str, str] | None = None) -> list[Call]:
    contacts = contacts or {}
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        cols = _columns(db, "ZCALLRECORD")
        if not cols:
            raise BackupError("The call history database has no call records table.")

        def col(name, default="NULL"):
            return name if name in cols else default

        rows = db.execute(
            f"""SELECT ZDATE, {col('ZADDRESS')}, {col('ZNAME')}, {col('ZORIGINATED', 0)},
                       {col('ZANSWERED', 0)}, {col('ZDURATION', 0)}, {col('ZCALLTYPE')},
                       {col('ZSERVICE_PROVIDER')}, {col('ZISO_COUNTRY_CODE')}
                FROM ZCALLRECORD ORDER BY ZDATE DESC"""
        ).fetchall()
    finally:
        db.close()

    calls = []
    for date, address, name, originated, answered, duration, call_type, provider, country in rows:
        if date is None:
            continue
        if isinstance(address, bytes):
            address = address.decode("utf-8", "replace")
        address = address or ""
        if originated:
            direction = OUTGOING
        elif answered:
            direction = INCOMING
        else:
            direction = MISSED
        calls.append(
            Call(
                when=(_APPLE_EPOCH + timedelta(seconds=float(date))).astimezone(),
                number=address,
                name=contacts.get(number_key(address)) or (name or ""),
                direction=direction,
                duration=int(round(duration or 0)),
                service=_service(call_type, provider),
                country=(country or "").upper(),
            )
        )
    return calls


def read_calls(backup: Backup) -> list[Call]:
    """All calls in the backup, newest first, with names from the backed-up contacts."""
    call_db = backup.extract_sqlite(*CALL_HISTORY)
    if not call_db:
        raise BackupError(
            "No call history in this backup. Make sure it's a full backup of the iPhone "
            "(not an iCloud-only device) and try backing up again."
        )
    contacts = {}
    book = backup.extract_sqlite(*ADDRESS_BOOK)
    if book:
        contacts = load_contacts(book)
    return read_call_db(call_db, contacts)


def filter_calls(calls, start=None, end=None, search: str = "", directions=None):
    """start/end are dates (inclusive); search matches name or number."""
    search = search.strip().lower()
    digits = number_key(search) if re.search(r"\d", search) else ""
    out = []
    for c in calls:
        d = c.when.date()
        if start and d < start:
            continue
        if end and d > end:
            continue
        if directions and c.direction not in directions:
            continue
        if search and search not in c.name.lower() and not (digits and digits in re.sub(r"\D", "", c.number)):
            continue
        out.append(c)
    return out
