"""The point-and-click app: a small server on 127.0.0.1 plus a page in your browser.

Nothing leaves the computer. The server only answers requests addressed to its own
localhost host name and carrying the per-run token embedded in the page, so other
websites open in the browser can't talk to it.
"""

from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
import tempfile
import threading
import webbrowser
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path

from . import __version__
from .backup import Backup, BackupError, PasswordRequired, WrongPassword, default_backup_dirs, find_backups, read_info
from .calls import filter_calls, read_calls
from .export import WRITERS

_TYPES = {
    "csv": "text/csv; charset=utf-8",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "html": "text/html; charset=utf-8",
}


class _State:
    def __init__(self):
        self.token = secrets.token_urlsafe(24)
        self.calls = []
        self.device = ""
        self.lock = threading.Lock()
        self.window = None  # pywebview window when running as a desktop app
        self.saved: set[str] = set()  # files we wrote, the only ones "Show in Finder" may reveal


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
            except (ValueError, json.JSONDecodeError):
                return self._json(400, {"error": "Bad request"})
            route = {
                "/api/backups": self.backups,
                "/api/open": self.open_backup,
                "/api/export": self.export,
                "/api/pick-folder": self.pick_folder,
                "/api/reveal": self.reveal,
                "/api/quit": self.quit,
            }.get(self.path)
            if not route:
                return self._send(404, b"Not found", "text/plain")
            try:
                route(body)
            except Exception as e:  # keep the app alive and show the problem
                self._json(500, {"error": f"Unexpected error: {e}"})

        def backups(self, body):
            self._json(
                200,
                [
                    {
                        "path": str(b.path),
                        "name": b.device_name,
                        "product": b.product,
                        "ios": b.ios_version,
                        "date": b.last_backup.isoformat() if b.last_backup else None,
                        "encrypted": b.encrypted,
                    }
                    for b in find_backups()
                ],
            )

        def pick_folder(self, body):
            if not state.window:
                return self._json(400, {"error": "Folder picker is only available in the app window."})
            import webview

            start = next((str(d) for d in default_backup_dirs() if d.is_dir()), str(Path.home()))
            picked = state.window.create_file_dialog(webview.FileDialog.FOLDER, directory=start)
            self._json(200, {"path": picked[0] if picked else None})

        def reveal(self, body):
            path = str(body.get("path") or "")
            if path not in state.saved or not os.path.exists(path):
                return self._json(404, {"error": "File not found"})
            if sys.platform == "darwin":
                subprocess.run(["open", "-R", path], check=False)
            elif sys.platform.startswith("win"):
                subprocess.run(["explorer", "/select,", path], check=False)
            else:
                subprocess.run(["xdg-open", str(Path(path).parent)], check=False)
            self._json(200, {"ok": True})

        def open_backup(self, body):
            path = Path(str(body.get("path") or "")).expanduser()
            try:
                info = read_info(path)
                with Backup(path, body.get("password") or None) as backup:
                    calls = read_calls(backup)
            except PasswordRequired as e:
                return self._json(401, {"error": str(e), "needPassword": True})
            except WrongPassword as e:
                return self._json(401, {"error": str(e), "needPassword": True})
            except BackupError as e:
                return self._json(400, {"error": str(e)})
            with state.lock:
                state.calls = calls
                state.device = info.device_name
            self._json(200, {"device": info.device_name, "path": str(info.path), "calls": _calls_json(calls)})

        def _filtered(self, body):
            def day(v):
                return date.fromisoformat(v) if v else None

            with state.lock:
                calls = state.calls
            return filter_calls(
                calls,
                day(body.get("start")),
                day(body.get("end")),
                body.get("search") or "",
                set(body.get("directions") or []) or None,
            )

        def export(self, body):
            fmt = body.get("format")
            if fmt not in WRITERS:
                return self._json(400, {"error": "Unknown format"})
            calls = self._filtered(body)
            name = f"call-history-{date.today():%Y-%m-%d}.{fmt}"
            if state.window:
                import webview

                picked = state.window.create_file_dialog(
                    webview.FileDialog.SAVE, directory=str(Path.home() / "Documents"), save_filename=name
                )
                if not picked:
                    return self._json(200, {"cancelled": True})
                out = Path(picked if isinstance(picked, str) else picked[0])
                if out.suffix.lower() != f".{fmt}":
                    out = out.with_name(out.name + f".{fmt}")
                self._write(fmt, calls, out)
                state.saved.add(str(out))
                return self._json(200, {"path": str(out), "count": len(calls)})
            with tempfile.TemporaryDirectory() as tmp:
                out = Path(tmp) / name
                self._write(fmt, calls, out)
                data = out.read_bytes()
            self._send(200, data, _TYPES[fmt], {"Content-Disposition": f'attachment; filename="{name}"'})

        def _write(self, fmt, calls, out):
            if fmt == "html":
                WRITERS[fmt](calls, out, title=f"Call history – {state.device}")
            else:
                WRITERS[fmt](calls, out)

        def quit(self, body):
            self._json(200, {"ok": True})
            if state.window:
                state.window.destroy()
            else:
                threading.Thread(target=server_ref["server"].shutdown, daemon=True).start()

    return Handler


def serve(port: int = 0, open_browser: bool = True, window: bool = True) -> None:
    """Open Ringlog in its own window (pywebview), or in the browser if that isn't available."""
    state = _State()
    ref: dict = {}
    server = ThreadingHTTPServer(("127.0.0.1", port), _make_handler(state, ref))
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
        state.window = webview.create_window(
            "Ringlog", url, width=1120, height=720, min_size=(820, 520), background_color="#f5f5f7"
        )
        try:
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
