# Ringlog

**Export your iPhone call history to Excel, CSV or a readable report. Free, open source, and it never leaves your computer.**

Ringlog reads the call history out of an iPhone backup you've made with Finder (Mac), or iTunes / Apple Devices (Windows). It fills in names from the contacts in the same backup and gives you:

- **Excel (.xlsx)**: every call, plus a summary sheet
- **CSV**: for any spreadsheet or script
- **HTML report**: totals, calls per day, missed calls by hour, busiest weekdays, most frequent contacts and longest calls

Each call has its date, time, name, number, direction (incoming, outgoing or missed), duration, and service (Phone, FaceTime, WhatsApp and other apps that use iOS calling).

Encrypted backups work too: you enter the backup password and Ringlog decrypts what it needs in a temporary folder, then deletes it.

## 1. Back up your iPhone

1. Connect the iPhone with a cable, unlock it and tap **Trust This Computer**.
2. **Mac:** open Finder, select the iPhone in the sidebar, choose *Back up all of the data on your iPhone to this Mac*, then **Back Up Now**.
   **Windows:** install **Apple Devices** from the Microsoft Store (or iTunes), select the iPhone, then **Back Up Now**.
3. Wait for it to finish.

If you tick *Encrypt local backup*, remember the password. Ringlog needs it, and Apple can't recover it.

## 2. Run Ringlog

### Download (no install)

Get `Ringlog-macOS.zip` or `Ringlog-Windows.zip` from the [Releases](../../releases) page and unzip it. On a Mac, drag **Ringlog.app** into Applications.

- **macOS:** the first time, right-click Ringlog → **Open** (the app isn't signed with a paid Apple certificate). To see your backups, give Ringlog **Full Disk Access** in System Settings → Privacy & Security, then reopen it.
- **Windows:** if SmartScreen warns you, click **More info → Run anyway**.

### Or with Python 3.9+

```bash
pip install "ringlog[window] @ git+https://github.com/mdsherinoff/ringlog"
```

```bash
ringlog
```

## 3. Browse and export

Ringlog opens in its own window, laid out like a Mac app:

- **Sidebar:** your backups, plus *All calls*, *Missed*, *Incoming* and *Outgoing* with counts.
- **Toolbar:** switch between **List** and **Insights**, pick a date range, search by name or number, and **Export**.
- **List:** a sortable table of every call. Click a column header to sort, and use the arrow keys to move through rows.
- **Insights:** totals, calls per day or week, missed calls by hour, busiest weekdays, most frequent contacts and longest calls.

Export saves exactly what you're looking at as Excel, CSV or an HTML report, and **Show in Finder** takes you to the file.

Shortcuts: <kbd>⌘O</kbd> open a backup · <kbd>⌘F</kbd> search · <kbd>⌘E</kbd> export · <kbd>Esc</kbd> close. On Windows, use <kbd>Ctrl</kbd>.

Backups kept somewhere else, such as an external drive, can be opened with **Open backup…**, or listed automatically by setting `RINGLOG_BACKUP_DIR` to that folder. `ringlog app --browser` uses your web browser instead of a window.

### Command line

```bash
ringlog list
```

```bash
ringlog export calls.xlsx
```

```bash
ringlog export report.html --since 2026-01-01 --until 2026-03-31 --only missed
```

Options: `--backup PATH` (default: the newest backup), `--password` (asked for if needed), `--search TEXT`, `--format csv|xlsx|html`.

## Privacy

- Ringlog works fully offline. It makes no network requests and has no analytics.
- The app is a small web server bound to `127.0.0.1` only. It accepts requests only from its own page, using a random token created each time it starts.
- Your backup is never changed. Decrypted copies live in a temporary folder that is deleted when you're done.
- Exported files contain personal data. Keep them somewhere private.

## Limits

- Needs a local backup on a computer. iCloud backups can't be read.
- iOS 10 or later (backups with `Manifest.db`).
- The iPhone keeps a limited call history, so very old calls may already be gone from the phone and therefore from the backup.
- Names come from contacts stored on the phone. A number without a contact shows only the number.

## How it works

Backups store each file under the SHA-1 of its domain and path. Ringlog looks up `HomeDomain/Library/CallHistoryDB/CallHistory.storedata` (table `ZCALLRECORD`) and `HomeDomain/Library/AddressBook/AddressBook.sqlitedb` in `Manifest.db`. For encrypted backups it unlocks the keybag in `Manifest.plist` with the password (PBKDF2, then AES key unwrap), decrypts `Manifest.db`, then each file with its own wrapped key. See [`src/ringlog/backup.py`](src/ringlog/backup.py).

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
```

```bash
.venv/bin/pytest
```

The tests build fake plain and encrypted backups ([`tests/fakebackup.py`](tests/fakebackup.py)), so you don't need an iPhone. Pushing a tag like `v0.1.0` builds the macOS and Windows downloads with GitHub Actions and attaches them to a release.

Bug reports and pull requests are welcome. Please never attach a real backup or export to an issue.

## License

[MIT](LICENSE). Not affiliated with or endorsed by Apple. iPhone, FaceTime and iTunes are trademarks of Apple Inc.
