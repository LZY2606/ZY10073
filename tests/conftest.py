from __future__ import annotations

import json
import socket
import time
import urllib.error
import urllib.request
from contextlib import closing

import pytest

from citeweave.server import create_server
from citeweave.storage import Store


@pytest.fixture
def store(tmp_path):
    database = Store(tmp_path / "citeweave.db")
    yield database
    database.close()


@pytest.fixture
def server(store):
    with closing(socket.socket()) as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    httpd = create_server("127.0.0.1", port, store)
    thread = __import__("threading").Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    httpd.shutdown()
    httpd.server_close()
    thread.join(timeout=2)


def request_json(base_url: str, path: str, method: str = "GET", body=None, headers=None):
    data = None
    request_headers = headers or {}
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        request_headers = {"Content-Type": "application/json", **request_headers}
    request = urllib.request.Request(
        base_url + path, data=data, headers=request_headers, method=method
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))
