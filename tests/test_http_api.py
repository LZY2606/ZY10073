"""End-to-end HTTP coverage for the required acceptance surface."""
import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from citeweave.db import DB
from citeweave.web.server import Handler
from .conftest import ALPHA_2020, ALPHA_2023, BETA_2019


@pytest.fixture
def client(tmp_path):
    db = DB(tmp_path / "http.sqlite3")
    from citeweave.service import CiteWeaveService
    CiteWeaveService(db).import_version(**ALPHA_2020)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    Handler.service = CiteWeaveService(db)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"

    def call(method, path, payload=None, raw=False):
        data = None if payload is None else json.dumps(payload).encode()
        req = urllib.request.Request(base + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req) as resp:
                body = resp.read()
                return resp.status, body if raw else json.loads(body)
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    yield call
    httpd.shutdown()


def test_duplicate_post_is_idempotent_over_http(client):
    code1, body1 = client("POST", "/api/imports", ALPHA_2020)
    code2, body2 = client("POST", "/api/imports", ALPHA_2020)
    assert code1 == 200 and body1["replayed"] is True  # fixture already seeded


def test_illegal_transition_http_status(client):
    code, body = client("POST", "/api/versions/1/transition",
                        {"state": "repealed"})
    assert code == 200
    code, body = client("POST", "/api/versions/1/transition",
                        {"state": "effective"})
    assert code == 409 and body["error"] == "illegal_transition"
    assert body["allowed"] == []


def test_conflict_returns_both_sides_http(client):
    cid = client("GET", "/api/graph?date=2020-06-01")[1]["edges"][0]["id"]
    code, _ = client("POST", f"/api/citations/{cid}/decisions", {
        "researcher": "alice", "action": "override",
        "target_version_id": 1, "source_page": "p1", "base_rev": 0})
    assert code == 200
    code, body = client("POST", f"/api/citations/{cid}/decisions", {
        "researcher": "bob", "action": "override",
        "target_version_id": 1, "base_rev": 0})
    assert code == 409
    assert body["current"]["researcher"] == "alice"
    assert body["base"]["researcher"] == "bob"


def test_export_and_static_index(client):
    code, body = client("GET", "/api/export", raw=True)
    assert code == 200 and body[:2] == b"PK"
    code, html = client("GET", "/", raw=True)
    assert code == 200 and b"CiteWeave" in html
