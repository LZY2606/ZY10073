"""stdlib HTTP server: JSON API + static UI.  Rules stay in the service."""
from __future__ import annotations

import json
import threading
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from ..models import (CiteWeaveError, Conflict, IllegalTransition, NotFound,
                      ValidationError)
from ..service import CiteWeaveService

STATIC = Path(__file__).parent / "static"

ERROR_STATUS = {
    "not_found": 404,
    "conflict": 409,
    "illegal_transition": 409,
    "validation_error": 400,
    "staged_crash": 500,
}


class Handler(BaseHTTPRequestHandler):
    service: CiteWeaveService = None  # type: ignore[assignment]

    server_version = "CiteWeave/0.1"

    def log_message(self, fmt, *args):  # quiet
        return

    # ---------- helpers ----------
    def _json(self, status: int, payload) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if not length:
            return {}
        try:
            data = json.loads(self.rfile.read(length).decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise ValidationError(f"invalid JSON: {exc}")
        if not isinstance(data, dict):
            raise ValidationError("request body must be a JSON object")
        return data

    def _error(self, exc: CiteWeaveError) -> None:
        payload = {"error": exc.code, "message": str(exc)}
        for key, value in getattr(exc, "extra", {}).items():
            if value is not None:
                payload[key] = value
        self._json(ERROR_STATUS.get(exc.code, 500), payload)

    # ---------- routing ----------
    def do_GET(self) -> None:  # noqa: N802
        try:
            self._route_get()
        except CiteWeaveError as exc:
            self._error(exc)

    def do_POST(self) -> None:  # noqa: N802
        try:
            self._route_post()
        except CiteWeaveError as exc:
            self._error(exc)

    def _route_get(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        svc = self.service
        if path == "/api/documents":
            return self._json(200, {"documents": svc.list_documents()})
        if path == "/api/versions":
            return self._json(200, {"versions": svc.list_versions(
                query.get("code"))})
        if path.startswith("/api/versions/") and path.endswith("/raw"):
            vid = int(path.split("/")[3])
            version = svc.get_version(vid)
            self.send_response(200)
            body = version["raw_text"].encode("utf-8")
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            return self.wfile.write(body)
        if path.startswith("/api/citations/"):
            cid = int(path.rsplit("/", 1)[-1])
            return self._json(200, svc.citation_detail(cid))
        if path == "/api/graph":
            date = query.get("date")
            if not date:
                raise ValidationError("date is required")
            return self._json(200, svc.graph_at(date))
        if path == "/api/diff":
            if not query.get("a") or not query.get("b"):
                raise ValidationError("a and b dates are required")
            return self._json(200, svc.date_diff(query["a"], query["b"]))
        if path == "/api/snapshots":
            return self._json(200, {"snapshots": svc.list_snapshots()})
        if path.startswith("/api/snapshots/"):
            sid = int(path.rsplit("/", 1)[-1])
            return self._json(200, svc.get_snapshot(sid))
        if path.startswith("/api/merges/"):
            mid = int(path.rsplit("/", 1)[-1])
            return self._json(200, svc.get_merge_session(mid))
        if path == "/api/recovery":
            return self._json(200, svc.recovery_status())
        if path == "/api/anchors/verify":
            return self._json(200, {"anchors": svc.verify_anchors()})
        if path == "/api/export":
            bundle = svc.build_bundle()
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Disposition",
                             'attachment; filename="citeweave-bundle.zip"')
            self.send_header("Content-Length", str(len(bundle)))
            self.end_headers()
            return self.wfile.write(bundle)
        return self._static(path)

    def _route_post(self) -> None:
        path = urlparse(self.path).path
        data = self._read_json()
        svc = self.service
        if path == "/api/imports":
            stage = data.pop("stage", "final")
            result = svc.import_version(stage=stage, **data)
            return self._json(result["status"], result)
        if path.startswith("/api/versions/") and path.endswith("/finalize"):
            vid = int(path.split("/")[3])
            result = svc.finalize_version(vid)
            return self._json(result["status"], result)
        if path.startswith("/api/versions/") and path.endswith("/transition"):
            vid = int(path.split("/")[3])
            result = svc.transition_version(vid, data["state"])
            return self._json(200, result)
        if path.startswith("/api/citations/") and path.endswith("/decisions"):
            cid = int(path.split("/")[3])
            result = svc.add_decision(citation_id=cid, **data)
            return self._json(200, result)
        if path == "/api/snapshots":
            return self._json(201, svc.create_snapshot(**data))
        if path.startswith("/api/snapshots/") and path.endswith("/publish"):
            sid = int(path.split("/")[3])
            return self._json(200, svc.publish_snapshot(sid, **data))
        if path == "/api/merges":
            return self._json(201, svc.create_merge_session(**data))
        if path.startswith("/api/merges/") and path.endswith("/merge"):
            mid = int(path.split("/")[3])
            return self._json(200, svc.merge_session(mid, **data))
        raise NotFound(f"unknown endpoint {path}")

    def _static(self, path: str) -> None:
        if path in ("", "/"):
            path = "/index.html"
        target = (STATIC / path.lstrip("/")).resolve()
        if not str(target).startswith(str(STATIC.resolve())) or not target.is_file():
            self.send_error(404)
            return
        ctype = ("application/javascript" if target.suffix == ".js" else
                 "text/html; charset=utf-8" if target.suffix == ".html" else
                 "text/css; charset=utf-8")
        body = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def serve(db, host: str, port: int) -> ThreadingHTTPServer:
    service = CiteWeaveService(db)
    handler = partial(Handler)
    # Bind service onto the class (single tenant workbench).
    Handler.service = service
    httpd = ThreadingHTTPServer((host, port), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd
