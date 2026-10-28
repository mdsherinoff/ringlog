"""Command line: `ringlog list`, `ringlog export`, `ringlog app`."""

from __future__ import annotations

import argparse
import getpass
import sys
from datetime import date
from pathlib import Path

from . import __version__
from .backup import Backup, BackupError, PasswordRequired, find_backups
from .calls import INCOMING, MISSED, OUTGOING, filter_calls, read_calls
from .export import WRITERS


def _pick_backup(arg: str | None) -> Path:
    if arg:
        return Path(arg).expanduser()
    backups = find_backups()
    if not backups:
        sys.exit("No iPhone backups found. Back up your iPhone with Finder or iTunes first, or pass --backup PATH.")
    if len(backups) > 1:
        print(f"Using the newest backup: {backups[0].label}", file=sys.stderr)
    return backups[0].path


def cmd_list(args) -> int:
    backups = find_backups()
    if not backups:
        print("No backups found in the default folders.")
        return 1
    for b in backups:
        print(f"{b.label}\n    {b.path}")
    return 0


def cmd_export(args) -> int:
    path = _pick_backup(args.backup)
    password = args.password
    try:
        try:
            backup = Backup(path, password)
        except PasswordRequired:
            password = getpass.getpass("Backup password: ")
            backup = Backup(path, password)
        with backup:
            calls = read_calls(backup)
    except BackupError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    directions = None
    if args.only:
        directions = {{"incoming": INCOMING, "outgoing": OUTGOING, "missed": MISSED}[d] for d in args.only}
    calls = filter_calls(calls, args.since, args.until, args.search or "", directions)

    out = Path(args.output)
    fmt = args.format or out.suffix.lstrip(".").lower()
    if fmt not in WRITERS:
        print(f"Error: unknown format '{fmt}'. Use .csv, .xlsx or .html.", file=sys.stderr)
        return 1
    WRITERS[fmt](calls, out)
    print(f"Wrote {len(calls)} calls to {out}")
    return 0


def cmd_app(args) -> int:
    from .web import serve

    serve(port=args.port, open_browser=not args.no_browser, window=not (args.browser or args.no_browser))
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="ringlog", description="Export iPhone call history from a local backup.")
    p.add_argument("--version", action="version", version=f"ringlog {__version__}")
    sub = p.add_subparsers(dest="command")

    sub.add_parser("list", help="show backups on this computer").set_defaults(func=cmd_list)

    e = sub.add_parser("export", help="export calls to CSV, Excel or an HTML report")
    e.add_argument("output", help="file to write, e.g. calls.xlsx, calls.csv or report.html")
    e.add_argument("--backup", help="backup folder (default: newest backup found)")
    e.add_argument("--password", help="backup password (prompted for if needed)")
    e.add_argument("--format", choices=sorted(WRITERS), help="override the format implied by the file name")
    e.add_argument("--since", type=date.fromisoformat, help="first day to include, YYYY-MM-DD")
    e.add_argument("--until", type=date.fromisoformat, help="last day to include, YYYY-MM-DD")
    e.add_argument("--search", help="only calls whose name or number contains this")
    e.add_argument("--only", nargs="+", choices=["incoming", "outgoing", "missed"])
    e.set_defaults(func=cmd_export)

    a = sub.add_parser("app", help="open the point-and-click app (default)")
    a.add_argument("--port", type=int, default=0)
    a.add_argument("--browser", action="store_true", help="use your web browser instead of an app window")
    a.add_argument("--no-browser", action="store_true", help="just start the server and print its address")
    a.set_defaults(func=cmd_app)

    args = p.parse_args(argv)
    if not args.command:
        args = p.parse_args(["app", *(argv or [])])
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
