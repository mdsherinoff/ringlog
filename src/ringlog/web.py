"""The point-and-click app: a small server on 127.0.0.1 plus a page shown in its own
window (pywebview) or, as a fallback, in the browser.

Nothing leaves the computer. The server only answers requests addressed to its own
localhost host name and carrying the per-run token embedded in the page, so other
websites open in a browser can't talk to it.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path

from . import __version__, prefs
from .backup import (
    Backup,
    BackupError,
    PasswordRequired,
    WrongPassword,
    blocked_backup_dirs,
    default_backup_dirs,
    find_backups,
    read_info,
)
from .calls import filter_calls, read_calls
from .export import WRITERS

REPO_URL = "https://github.com/mdsherinoff/ringlog"

_TYPES = {
    "csv": "text/csv; charset=utf-8",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "html": "text/html; charset=utf-8",
}

_PRIVACY_SETTINGS = "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles"


class _State:
    def __init__(self, settings: prefs.Prefs | None = None):
        self.token = secrets.token_urlsafe(24)
        self.calls = []
        self.device = ""
        self.lock = threading.Lock()
        self.window = None  # pywebview window when running as a desktop app
        self.saved: set[str] = set()  # files we wrote; with known backups, the only things we reveal
        self.cache: dict[str, tuple[float, str, list]] = {}  # path -> (Manifest.db mtime, device, calls)
        self.prefs = settings or prefs.Prefs()


def _calls_json(calls):
    return [
        {
            "when": c.when.isoformat(timespec="seconds"),
            "name": c.name,
            "number": c.number,
            "direction": c.direction,
            "duration": c.duration,
            "service": c.service,
        }
        for c in calls
    ]


def _safe_filename(text: str) -> str:
    return re.sub(r'[\\/:*?"<>|\x00-\x1f]+', "", text).strip() or "Call history"


def _export_name(device: str, calls, fmt: str) -> str:
    if calls:
        first = min(c.when for c in calls).date()
        last = max(c.when for c in calls).date()
        span = f"{first:%Y-%m-%d}" if first == last else f"{first:%Y-%m-%d} to {last:%Y-%m-%d}"
    else:
        span = f"{date.today():%Y-%m-%d}"
    return f"{_safe_filename(f'{device} calls {span}')}.{fmt}"


def _reveal(path: str) -> None:
    if sys.platform == "darwin":
        subprocess.run(["open", "-R", path], check=False)
    elif sys.platform.startswith("win"):
        subprocess.run(["explorer", "/select,", path], check=False)
    else:
        subprocess.run(["xdg-open", str(Path(path).parent)], check=False)


def _make_handler(state: _State, server_ref: dict):
    page = resources.files("ringlog").joinpath("app.html").read_text(encoding="utf-8")
    page = page.replace("__TOKEN__", state.token).replace("__VERSION__", __version__)
    page = page.replace("__PLATFORM__", sys.platform)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _host_ok(self) -> bool:
            port = self.server.server_address[1]
            return self.headers.get("Host") in (f"127.0.0.1:{port}", f"localhost:{port}")

        def _send(self, status, body: bytes, ctype="application/json", extra=None):
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status, obj):
            self._send(status, json.dumps(obj).encode())

        def do_GET(self):
            if not self._host_ok():
                return self._send(403, b"Forbidden", "text/plain")
            if self.path in ("/", "/index.html"):
                html = page.replace("__MODE__", "window" if state.window else "browser")
                return self._send(
                    200,
                    html.encode(),
                    "text/html; charset=utf-8",
                    {
                        "Content-Security-Policy": "default-src 'self'; img-src 'self' data:; "
                        "style-src 'unsafe-inline'; script-src 'unsafe-inline'"
                    },
                )
            self._send(404, b"Not found", "text/plain")

        def do_POST(self):
            if not self._host_ok() or self.headers.get("X-Ringlog-Token") != state.token:
                return self._send(403, b"Forbidden", "text/plain")
            try:
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                if not isinstance(body, dict):
                    raise ValueError
            except (ValueError, json.JSONDecodeError):
                return self._json(400, {"error": "Bad request"})
            route = {
                "/api/backups": self.backups,
                "/api/open": self.open_backup,
                "/api/forget": self.forget,
                "/api/export": self.export,
                "/api/pick-folder": self.pick_folder,
                "/api/reveal": self.reveal,
                "/api/prefs": self.prefs,
                "/api/open-settings": self.open_settings,
                "/api/open-link": self.open_link,
                "/api/quit": self.quit,
            }.get(self.path)
            if not route:
                return self._send(404, b"Not found", "text/plain")
            try:
                route(body)
            except Exception as e:  # keep the app alive and show the problem
                self._json(500, {"error": f"Something went wrong: {e}"})

        # --- Backups -------------------------------------------------------

        def backups(self, body):
            remembered = state.prefs.remembered()
            self._json(
                200,
                {
                    "backups": [
                        {
                            "path": str(b.path),
                            "id": b.id,
                            "name": b.device_name,
                            "product": b.product,
                            "ios": b.ios_version,
                            "date": b.last_backup.isoformat() if b.last_backup else None,
                            "encrypted": b.encrypted,
                            "remembered": b.id in remembered,
                        }
                        for b in find_backups()
                    ],
                    "blocked": [str(d) for d in blocked_backup_dirs()],
                    "keychain": prefs.keychain_available(),
                },
            )

        def open_backup(self, body):
            path = Path(str(body.get("path") or "")).expanduser()
            password = body.get("password") or None
            remember = bool(body.get("remember"))
            try:
                info = read_info(path)
            except PermissionError:
                return self._json(403, {"error": "Ringlog isn't allowed to read this folder.", "blocked": True})
            except (BackupError, OSError) as e:
                return self._json(400, {"error": str(e)})

            key = str(info.path.resolve())
            try:
                mtime = (info.path / "Manifest.db").stat().st_mtime
            except OSError:
                mtime = 0
            cached = state.cache.get(key)
            used_saved = False
            if cached and cached[0] == mtime and not password:
                device, calls = cached[1], cached[2]
            else:
                if info.encrypted and not password:
                    password = prefs.load_password(info.id)
                    used_saved = bool(password)
                try:
                    with Backup(info.path, password) as backup:
                        calls = read_calls(backup)
                except PasswordRequired as e:
                    return self._json(401, {"error": str(e), "needPassword": True})
                except WrongPassword as e:
                    if used_saved:  # the saved password is stale; drop it and ask
                        prefs.forget_password(info.id)
                        state.prefs.set_remembered(info.id, False)
                        return self._json(401, {"error": "", "needPassword": True})
                    return self._json(401, {"error": str(e), "needPassword": True, "wrong": True})
                except PermissionError:
                    return self._json(403, {"error": "Ringlog isn't allowed to read this backup.", "blocked": True})
                except BackupError as e:
                    return self._json(400, {"error": str(e)})
                device = info.device_name
                state.cache[key] = (mtime, device, calls)
                if info.encrypted and password and remember and not used_saved:
                    if prefs.save_password(info.id, password):
                        state.prefs.set_remembered(info.id, True)

            with state.lock:
                state.calls = calls
                state.device = device
            state.prefs.update({"last_backup": str(info.path)})
            self._json(
                200,
                {
                    "device": device,
                    "path": str(info.path),
                    "id": info.id,
                    "remembered": info.id in state.prefs.remembered(),
                    "calls": _calls_json(calls),
                },
            )

        def forget(self, body):
            backup_id = str(body.get("id") or "")
            prefs.forget_password(backup_id)
            state.prefs.set_remembered(backup_id, False)
            self._json(200, {"ok": True})

        def pick_folder(self, body):
            if not state.window:
                return self._json(400, {"error": "Folder picker is only available in the app window."})
            import webview

            start = next((str(d) for d in default_backup_dirs() if d.is_dir()), str(Path.home()))
            picked = state.window.create_file_dialog(webview.FileDialog.FOLDER, directory=start)
            self._json(200, {"path": picked[0] if picked else None})

        # --- Export --------------------------------------------------------

        def _selected(self, body):
            with state.lock:
                calls = state.calls
            ids = body.get("ids")
            if isinstance(ids, list):
                return [calls[i] for i in ids if isinstance(i, int) and 0 <= i < len(calls)]

            def day(v):
                return date.fromisoformat(v) if v else None

            return filter_calls(
                calls,
                day(body.get("start")),
                day(body.get("end")),
                body.get("search") or "",
                set(body.get("directions") or []) or None,
            )

        def _write(self, fmt, calls, out):
            if fmt == "html":
                WRITERS[fmt](calls, out, title=f"Call history – {state.device}")
            else:
                WRITERS[fmt](calls, out)

        def export(self, body):
            fmt = body.get("format")
            if fmt not in WRITERS:
                return self._json(400, {"error": "Unknown format"})
            calls = self._selected(body)
            name = _export_name(state.device, calls, fmt)
            if state.window:
                import webview

                folder = state.prefs.get("export_dir")
                if not folder or not Path(folder).is_dir():
                    folder = str(Path.home() / "Documents")
                picked = state.window.create_file_dialog(webview.FileDialog.SAVE, directory=folder, save_filename=name)
                if not picked:
                    return self._json(200, {"cancelled": True})
                out = Path(picked if isinstance(picked, str) else picked[0])
                if out.suffix.lower() != f".{fmt}":
                    out = out.with_name(out.name + f".{fmt}")
                try:
                    self._write(fmt, calls, out)
                except PermissionError:
                    return self._json(400, {"error": f"Ringlog can't save to {out.parent}. Choose another folder."})
                except OSError as e:
                    return self._json(400, {"error": f"Couldn't save the file: {e.strerror or e}"})
                state.saved.add(str(out))
                state.prefs.update({"export_dir": str(out.parent)})
                return self._json(200, {"path": str(out), "count": len(calls)})
            with tempfile.TemporaryDirectory() as tmp:
                out = Path(tmp) / name
                self._write(fmt, calls, out)
                data = out.read_bytes()
            ascii_name = name.encode("ascii", "replace").decode().replace("?", "_")
            self._send(
                200,
                data,
                _TYPES[fmt],
                {"Content-Disposition": f"attachment; filename=\"{ascii_name}\"", "X-Filename": json.dumps(name)},
            )

        # --- Desktop helpers ---------------------------------------------

        def reveal(self, body):
            path = str(body.get("path") or "")
            known = {str(b.path) for b in find_backups()}
            if (path not in state.saved and path not in known) or not os.path.exists(path):
                return self._json(404, {"error": "File not found"})
            _reveal(path)
            self._json(200, {"ok": True})

        def prefs(self, body):
            if isinstance(body.get("set"), dict):
                state.prefs.update({k: v for k, v in body["set"].items() if k in ("view", "sort")})
            self._json(
                200,
                {
                    "last_backup": state.prefs.get("last_backup"),
                    "view": state.prefs.get("view", "list"),
                    "sort": state.prefs.get("sort"),
                },
            )

        def open_settings(self, body):
            if sys.platform == "darwin":
                subprocess.run(["open", _PRIVACY_SETTINGS], check=False)
            self._json(200, {"ok": True})

        def open_link(self, body):
            links = {"repo": REPO_URL, "help": REPO_URL + "#1-back-up-your-iphone", "issues": REPO_URL + "/issues"}
            url = links.get(body.get("link"))
            if url:
                webbrowser.open(url)
            self._json(200, {"ok": bool(url)})

        def quit(self, body):
            self._json(200, {"ok": True})
            if state.window:
                state.window.destroy()
            else:
                threading.Thread(target=server_ref["server"].shutdown, daemon=True).start()

    return Handler


def _clean_stale_temp() -> None:
    """Remove decrypted leftovers from a previous run that crashed or was force-quit."""
    cutoff = time.time() - 3600
    for d in Path(tempfile.gettempdir()).glob("ringlog-*"):
        try:
            if d.is_dir() and d.stat().st_mtime < cutoff:
                shutil.rmtree(d, ignore_errors=True)
        except OSError:
            pass


def _menu(state: _State):
    import webview.menu as wm

    def js(cmd):
        return lambda: state.window and state.window.evaluate_js(f"window.ringlog && ringlog.cmd({json.dumps(cmd)})")

    return [
        wm.Menu(
            "File",
            [
                wm.MenuAction("Open Backup…", js("open")),
                wm.MenuAction("Refresh Backups", js("refresh")),
                wm.MenuSeparator(),
                wm.MenuAction("Export as Excel…", js("export:xlsx")),
                wm.MenuAction("Export as CSV…", js("export:csv")),
                wm.MenuAction("Export Report…", js("export:html")),
            ],
        ),
        wm.Menu(
            "View",
            [
                wm.MenuAction("List", js("view:list")),
                wm.MenuAction("Insights", js("view:insights")),
                wm.MenuSeparator(),
                wm.MenuAction("All Calls", js("box:all")),
                wm.MenuAction("Missed Calls", js("box:Missed")),
                wm.MenuAction("Incoming Calls", js("box:Incoming")),
                wm.MenuAction("Outgoing Calls", js("box:Outgoing")),
                wm.MenuSeparator(),
                wm.MenuAction("Find…", js("find")),
            ],
        ),
        wm.Menu(
            "Help",
            [
                wm.MenuAction("How to Back Up Your iPhone", lambda: webbrowser.open(REPO_URL + "#1-back-up-your-iphone")),
                wm.MenuAction("Report a Problem", lambda: webbrowser.open(REPO_URL + "/issues")),
                wm.MenuAction("Ringlog on GitHub", lambda: webbrowser.open(REPO_URL)),
            ],
        ),
    ]


def serve(port: int = 0, open_browser: bool = True, window: bool = True) -> None:
    """Open Ringlog in its own window (pywebview), or in the browser if that isn't available."""
    _clean_stale_temp()
    state = _State()
    ref: dict = {}
    server = ThreadingHTTPServer(("127.0.0.1", port), _make_handler(state, ref))
    server.daemon_threads = True
    ref["server"] = server
    url = f"http://127.0.0.1:{server.server_address[1]}/"

    webview = None
    if window:
        try:
            import webview
        except ImportError:
            webview = None

    if webview:
        threading.Thread(target=server.serve_forever, daemon=True).start()
        size = state.prefs.get("window") or {}
        state.window = webview.create_window(
            "Ringlog",
            url,
            width=int(size.get("width", 1120)),
            height=int(size.get("height", 720)),
            min_size=(820, 520),
            background_color="#f5f5f7",
        )

        def remember_size():
            try:
                state.prefs.update({"window": {"width": state.window.width, "height": state.window.height}})
            except Exception:
                pass

        state.window.events.closing += remember_size
        try:
            try:
                webview.start(menu=_menu(state))
            except TypeError:  # older pywebview without menus
                webview.start()
        finally:
            server.shutdown()
            server.server_close()
        return

    print(f"Ringlog is running at {url}\nKeep this window open while you use it. Press Ctrl+C to quit.")
    if open_browser:
        threading.Timer(0.3, webbrowser.open, args=(url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        print("Ringlog stopped.")


__all__ = ["serve"]
