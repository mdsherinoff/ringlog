import csv
import json
import os
import sys
import threading
import urllib.request
from http.server import ThreadingHTTPServer

import pytest
from openpyxl import load_workbook

from fakebackup import make_backup
from ringlog.backup import Backup, PasswordRequired, WrongPassword, find_backups
from ringlog.calls import MISSED, OUTGOING, filter_calls, read_calls
from ringlog.cli import main
from ringlog.export import to_csv, to_html, to_xlsx


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """Keep tests away from the real settings file and keychain."""
    from ringlog import backup, prefs

    store = {}
    monkeypatch.setattr(backup, "_apple_backup_dirs", lambda: [])
    monkeypatch.setattr(prefs, "config_dir", lambda: tmp_path / "config")
    monkeypatch.setattr(prefs, "keychain_available", lambda: True)
    monkeypatch.setattr(prefs, "load_password", lambda i: store.get(i))
    monkeypatch.setattr(prefs, "save_password", lambda i, pw: store.__setitem__(i, pw) or True)
    monkeypatch.setattr(prefs, "forget_password", lambda i: store.pop(i, None))
    monkeypatch.setenv("RINGLOG_BACKUP_DIR", str(tmp_path / "none"))
    return store


@pytest.fixture(params=[None, "s3cret"], ids=["plain", "encrypted"])
def backup_dir(tmp_path, request):
    return make_backup(tmp_path / "ABC123", request.param), request.param


def test_reads_calls_with_contact_names(backup_dir):
    path, pw = backup_dir
    with Backup(path, pw) as b:
        calls = read_calls(b)
    assert len(calls) == 5
    newest = calls[0]
    assert newest.name == "Corner Shop" and newest.service == "WhatsApp"
    # +44 7700 900123 and 07700900123 both match the contact saved as +44 (7700) 900-123
    alex = [c for c in calls if c.name == "Alex Morgan"]
    assert len(alex) == 2
    first = calls[-1]
    assert first.direction == OUTGOING and first.duration == 125 and first.duration_text == "2:05"
    assert [c.direction for c in calls].count(MISSED) == 1
    facetime = next(c for c in calls if c.number == "someone@example.com")
    assert facetime.service == "FaceTime Audio"


def test_encrypted_needs_right_password(tmp_path):
    path = make_backup(tmp_path / "enc", "right")
    with pytest.raises(PasswordRequired):
        Backup(path)
    with pytest.raises(WrongPassword):
        Backup(path, "wrong")


def test_find_backups(tmp_path):
    make_backup(tmp_path / "one")
    make_backup(tmp_path / "two", "pw")
    (tmp_path / "junk").mkdir()
    found = find_backups([tmp_path])
    assert {b.path.name for b in found} == {"one", "two"}
    assert {b.encrypted for b in found} == {True, False}
    assert "Test iPhone" in found[0].label


def test_filter(backup_dir):
    path, pw = backup_dir
    with Backup(path, pw) as b:
        calls = read_calls(b)
    assert len(filter_calls(calls, directions={MISSED})) == 1
    assert len(filter_calls(calls, search="alex")) == 2
    assert len(filter_calls(calls, search="0100 222")) == 1


def test_exports(tmp_path):
    path = make_backup(tmp_path / "b")
    with Backup(path) as b:
        calls = read_calls(b)
    to_csv(calls, tmp_path / "c.csv")
    rows = list(csv.reader(open(tmp_path / "c.csv", encoding="utf-8-sig")))
    assert rows[0][0] == "Date" and len(rows) == 6

    to_xlsx(calls, tmp_path / "c.xlsx")
    wb = load_workbook(tmp_path / "c.xlsx")
    assert wb["Calls"].max_row == 6
    assert wb["Summary"]["B2"].value == 5

    to_html(calls, tmp_path / "r.html")
    page = (tmp_path / "r.html").read_text()
    assert "Most frequent contacts" in page and "Alex Morgan" in page


def test_cli_export(tmp_path, capsys):
    path = make_backup(tmp_path / "b", "pw")
    out = tmp_path / "out.xlsx"
    assert main(["export", str(out), "--backup", str(path), "--password", "pw", "--only", "missed"]) == 0
    assert load_workbook(out)["Calls"].max_row == 2
    assert main(["export", str(tmp_path / "x.txt"), "--backup", str(path), "--password", "pw"]) == 1


def test_web_api(tmp_path):
    from ringlog.web import _make_handler, _State

    path = make_backup(tmp_path / "b", "pw")
    state, ref = _State(), {}
    server = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(state, ref))
    ref["server"] = server
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"

    def post(route, body, token=state.token):
        req = urllib.request.Request(base + route, json.dumps(body).encode(), {"X-Ringlog-Token": token}, method="POST")
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    try:
        assert state.token in urllib.request.urlopen(base + "/").read().decode()
        assert post("/api/open", {"path": str(path)}, token="nope")[0] == 403
        status, body = post("/api/open", {"path": str(path)})
        assert status == 401 and json.loads(body)["needPassword"]
        status, body = post("/api/open", {"path": str(path), "password": "pw"})
        assert status == 200 and len(json.loads(body)["calls"]) == 5
        status, body = post("/api/export", {"format": "csv", "directions": ["Missed"]})
        assert status == 200 and body.decode("utf-8-sig").count("\n") == 2
    finally:
        server.shutdown()


def test_web_window_mode_saves_with_dialog(tmp_path, monkeypatch):
    pytest.importorskip("webview")
    import ringlog.web as web

    path = make_backup(tmp_path / "b")
    target = tmp_path / "picked" / "my calls"
    target.parent.mkdir()

    class FakeWindow:
        def create_file_dialog(self, kind, **kw):
            return str(target) if kw.get("save_filename") else [str(path)]

    revealed = []
    monkeypatch.setattr(web.subprocess, "run", lambda args, **kw: revealed.append(args))
    state, ref = web._State(), {}
    state.window = FakeWindow()
    server = ThreadingHTTPServer(("127.0.0.1", 0), web._make_handler(state, ref))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"

    def post(route, body):
        req = urllib.request.Request(base + route, json.dumps(body).encode(), {"X-Ringlog-Token": state.token}, method="POST")
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    try:
        assert 'MODE = "window"' in urllib.request.urlopen(base + "/").read().decode()
        assert post("/api/pick-folder", {}) == (200, {"path": str(path)})
        assert post("/api/open", {"path": str(path)})[0] == 200
        status, res = post("/api/export", {"format": "xlsx"})
        assert status == 200 and res["path"].endswith("my calls.xlsx") and res["count"] == 5
        assert load_workbook(res["path"])["Calls"].max_row == 6
        assert post("/api/reveal", {"path": res["path"]})[0] == 200 and revealed
        assert post("/api/reveal", {"path": str(path / "Manifest.plist")})[0] == 404
    finally:
        server.shutdown()


def _server(state):
    from ringlog.web import _make_handler

    server = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(state, {"server": None}))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"

    def post(route, body=None):
        req = urllib.request.Request(base + route, json.dumps(body or {}).encode(), {"X-Ringlog-Token": state.token}, method="POST")
        try:
            with urllib.request.urlopen(req) as r:
                data = r.read()
                return r.status, (json.loads(data) if r.headers.get("Content-Type", "").startswith("application/json") else data), r.headers
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read()), e.headers

    return server, post


def test_remember_password_cache_and_forget(tmp_path, monkeypatch, isolated):
    from ringlog.web import _State

    root = tmp_path / "backups"
    path = make_backup(root / "DEVICE1", "pw")
    monkeypatch.setenv("RINGLOG_BACKUP_DIR", str(root))
    state = _State()
    server, post = _server(state)
    try:
        status, res, _ = post("/api/backups")
        assert status == 200 and res["backups"][0]["encrypted"] and not res["backups"][0]["remembered"]
        assert res["blocked"] == [] and res["keychain"] is True

        assert post("/api/open", {"path": str(path), "password": "nope"})[1]["wrong"] is True
        status, res, _ = post("/api/open", {"path": str(path), "password": "pw", "remember": True})
        assert status == 200 and res["remembered"] and isolated["DEVICE1"] == "pw"
        assert post("/api/backups")[1]["backups"][0]["remembered"] is True
        assert post("/api/prefs")[1]["last_backup"] == str(path)

        # Opening again is served from the cache; a fresh app uses the saved password.
        assert post("/api/open", {"path": str(path)})[0] == 200
        server2, post2 = _server(_State())
        try:
            assert post2("/api/open", {"path": str(path)})[0] == 200
        finally:
            server2.shutdown()

        # A stale saved password is dropped and the user is asked again.
        isolated["DEVICE1"] = "old"
        server3, post3 = _server(_State())
        try:
            status, res, _ = post3("/api/open", {"path": str(path)})
            assert status == 401 and res["needPassword"] and "DEVICE1" not in isolated
        finally:
            server3.shutdown()

        isolated["DEVICE1"] = "pw"
        assert post("/api/forget", {"id": "DEVICE1"})[0] == 200
        assert "DEVICE1" not in isolated and not post("/api/backups")[1]["backups"][0]["remembered"]
    finally:
        server.shutdown()


def test_export_ids_order_and_filename(tmp_path):
    from ringlog.web import _State

    path = make_backup(tmp_path / "b")
    state = _State()
    server, post = _server(state)
    try:
        calls = post("/api/open", {"path": str(path)})[1]["calls"]
        status, body, headers = post("/api/export", {"format": "csv", "ids": [4, 0, 999, "x"]})
        lines = body.decode("utf-8-sig").strip().splitlines()
        assert status == 200 and len(lines) == 3
        assert lines[1].startswith(calls[4]["when"][:10]) and lines[2].startswith(calls[0]["when"][:10])
        assert json.loads(headers["X-Filename"]) == "Test iPhone calls 2026-10-01 to 2026-10-04.csv"
        assert post("/api/prefs", {"set": {"view": "insights", "last_backup": "/evil"}})[1]["view"] == "insights"
        assert post("/api/prefs")[1]["last_backup"] == str(path)
    finally:
        server.shutdown()


@pytest.mark.skipif(sys.platform.startswith("win") or os.geteuid() == 0, reason="needs POSIX permissions")
def test_blocked_dirs(tmp_path):
    from ringlog.backup import blocked_backup_dirs

    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0)
    try:
        assert blocked_backup_dirs([locked, tmp_path / "missing", tmp_path]) == [locked]
    finally:
        locked.chmod(0o755)


def test_prefs_file_roundtrip(tmp_path):
    from ringlog.prefs import Prefs

    p = Prefs(tmp_path / "x" / "prefs.json")
    p.update({"view": "insights", "junk": 1})
    p.set_remembered("A", True)
    q = Prefs(tmp_path / "x" / "prefs.json")
    assert q.get("view") == "insights" and q.get("junk") is None and q.remembered() == {"A"}
    (tmp_path / "x" / "prefs.json").write_text("not json")
    assert Prefs(tmp_path / "x" / "prefs.json").data == {}
