from __future__ import annotations

import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from typing import Any
from urllib.parse import parse_qs, urlparse

from .storage import ConflictError, IllegalTransitionError, NotFoundError, Store


def create_server(host: str, port: int, store: Store) -> ThreadingHTTPServer:
    def handler(*args: Any, **kwargs: Any) -> None:
        ApiHandler(store, *args, **kwargs)

    httpd = ThreadingHTTPServer((host, port), handler)
    httpd.daemon_threads = True
    return httpd


class ApiHandler(BaseHTTPRequestHandler):
    server_version = "CiteWeave/0.1"

    def __init__(self, store: Store, *args: Any, **kwargs: Any):
        self.store = store
        super().__init__(*args, **kwargs)

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _send_json(self, code: int, value: Any) -> None:
        body = json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, html: str) -> None:
        body = html.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            value = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid JSON: {exc.msg}") from exc
        if not isinstance(value, dict):
            raise ValueError("JSON body must be an object")
        return value

    def _query(self) -> dict[str, str]:
        parsed = parse_qs(urlparse(self.path).query)
        return {key: values[-1] for key, values in parsed.items()}

    def _idempotency_key(self, body: dict[str, Any]) -> str:
        return self.headers.get("Idempotency-Key")

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path.rstrip("/") or "/"
        try:
            if path == "/":
                html = resources.files("citeweave.web").joinpath("index.html").read_text(encoding="utf-8")
                self._send_html(html)
            elif path == "/api/health":
                self._send_json(200, {"ok": True})
            elif path == "/api/documents":
                self._send_json(200, {"documents": self.store.list_documents()})
            elif path.startswith("/api/documents/"):
                rest = path.removeprefix("/api/documents/").split("/")
                if len(rest) == 1:
                    self._send_json(200, self.store.get_document_detail(rest[0]))
                elif len(rest) == 2 and rest[1] == "timeline":
                    self._send_json(200, {"events": self.store.document_timeline(rest[0])})
                else:
                    self._send_json(404, {"error": "not found"})
            elif path == "/api/citations":
                self._send_json(200, {"citations": self.store.list_citations()})
            elif path.startswith("/api/citations/"):
                citation_id = path.removeprefix("/api/citations/").split("/", 1)[0]
                self._send_json(200, self.store.get_citation(citation_id))
            elif path == "/api/graph":
                query = self._query()
                self._send_json(200, self.store.graph_at(query.get("date", "1900-01-01")))
            elif path == "/api/compare":
                query = self._query()
                self._send_json(
                    200,
                    self.store.compare_graphs(
                        query.get("left", "1900-01-01"), query.get("right", "1900-01-01")
                    ),
                )
            elif path == "/api/chain":
                query = self._query()
                self._send_json(
                    200,
                    self.store.shortest_chain(
                        query["source_clause_id"],
                        query["target_clause_id"],
                        query.get("date", "1900-01-01"),
                    ),
                )
            elif path == "/api/timeline":
                self._send_json(200, self.store.timeline(self._query().get("date")))
            elif path == "/api/snapshots":
                self._send_json(200, {"snapshots": self.store.list_snapshots()})
            elif path.startswith("/api/snapshots/"):
                label = path.removeprefix("/api/snapshots/")
                self._send_json(200, self.store.get_snapshot(label))
            elif path == "/api/export":
                self._send_json(200, self.store.export_package())
            else:
                self._send_json(404, {"error": "not found"})
        except NotFoundError:
            self._send_json(404, {"error": "not found"})
        except (KeyError, ValueError) as exc:
            self._send_json(400, {"error": str(exc)})

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path.rstrip("/") or "/"
        try:
            body = self._read_json()
            idem_key = self._idempotency_key(body)
            cached = self.store.idempotent_get(idem_key)
            if cached:
                self._send_json(cached["code"], cached["body"])
                return
            response = self._dispatch_post(path, body)
            code, payload = response
            if code == 201 and isinstance(payload, dict) and payload.get("duplicate"):
                code = 200
            self.store.idempotent_put(idem_key, code, payload)
            self._send_json(code, payload)
        except NotFoundError:
            self._send_json(404, {"error": "not found"})
        except ConflictError as exc:
            self._send_json(409, {"error": str(exc), "difference": exc.difference})
        except IllegalTransitionError as exc:
            self._send_json(409, {"error": str(exc), "code": "illegal_transition"})
        except (KeyError, TypeError, ValueError) as exc:
            self._send_json(400, {"error": str(exc)})

    def _dispatch_post(self, path: str, body: dict[str, Any]) -> tuple[int, Any]:
        if path == "/api/documents":
            result = self.store.import_document(
                doc_key=body["doc_key"],
                version_label=body["version_label"],
                title=body.get("title"),
                raw_text=body["raw_text"],
                effective_date=body.get("effective_date"),
                repealed_date=body.get("repealed_date"),
                status=body.get("status", "ready"),
            )
            return (201 if not result["duplicate"] else 200), result
        if path.startswith("/api/documents/"):
            parts = path.removeprefix("/api/documents/").split("/")
            if len(parts) == 2 and parts[1] == "transition":
                document = self.store.transition_document(
                    parts[0],
                    body["status"],
                    expected_version=int(body["expected_version"]),
                    reason=body.get("reason", ""),
                )
                return 200, document
        if path.startswith("/api/citations/"):
            parts = path.removeprefix("/api/citations/").split("/")
            if len(parts) == 2 and parts[1] == "corrections":
                citation = self.store.correct_citation(
                    parts[0],
                    researcher=body["researcher"],
                    target_clause_id=body["target_clause_id"],
                    rationale=body.get("rationale", ""),
                    applies_from_version=body.get("applies_from_version"),
                    applies_to_version=body.get("applies_to_version"),
                    expected_version=int(body["expected_version"]),
                )
                return 201, citation
            if len(parts) == 2 and parts[1] == "merge":
                citation = self.store.merge_citation_corrections(
                    parts[0],
                    researcher=body["researcher"],
                    first=body["first"],
                    second=body["second"],
                    rationale=body.get("rationale", ""),
                )
                return 200, citation
            if len(parts) == 2 and parts[1] == "reparse":
                return 200, self.store.reparse_affected(body["citation_semantic_key"])
        if path == "/api/snapshots":
            return 201, self.store.publish_snapshot(
                body["label"], body["at_date"], body.get("created_by", "researcher")
            )
        if path == "/api/anchors/verify":
            return 200, self.store.verify_anchor_text(body["document_id"], body["raw_text"])
        raise NotFoundError(path)
