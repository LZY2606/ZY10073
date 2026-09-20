"""Published research snapshots: immutable once published."""
from __future__ import annotations

import hashlib
import json
from typing import Any

from .. import PARSER_VERSION
from ..db import utcnow
from ..models import Conflict, NotFound, check_transition, \
    LEGAL_SNAPSHOT_TRANSITIONS


def snapshot_payload(self, date: str, label: str,
                     researcher: str = "") -> dict[str, Any]:
    """Materialise the exact graph at ``date`` for freezing."""
    graph = self.graph_at(date)
    body = {
        "label": label,
        "researcher": researcher,
        "date": date,
        "parser_version": PARSER_VERSION,
        "created_at": utcnow(),
        "graph": graph,
    }
    return body


def _fingerprint(body: dict[str, Any]) -> str:
    blob = json.dumps(body, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))
    return "sha256:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()


def create_snapshot(self, *, date: str, label: str,
                    researcher: str = "") -> dict[str, Any]:
    body = self.snapshot_payload(date, label, researcher)
    body["state"] = "open"
    with self.db.lock:
        cur = self.db.conn.execute(
            "INSERT INTO snapshots(label,state,payload_fingerprint,body,"
            "parser_version,revision,created_at) VALUES(?,?,?,?,?,1,?)",
            (label, "open", _fingerprint(body),
             json.dumps(body, ensure_ascii=False), PARSER_VERSION, utcnow()))
        snapshot_id = cur.lastrowid
    return {"snapshot_id": snapshot_id, "state": "open", "label": label,
            "date": date}


def publish_snapshot(self, snapshot_id: int, base_rev: int) -> dict[str, Any]:
    with self.db.lock:
        row = self.db.one("SELECT * FROM snapshots WHERE id=?", (snapshot_id,))
        if row is None:
            raise NotFound(f"snapshot {snapshot_id} not found")
        if row["revision"] != base_rev:
            raise Conflict("snapshot revision is stale",
                           base=base_rev, current=row["revision"])
        check_transition(LEGAL_SNAPSHOT_TRANSITIONS, row["state"], "published")
        self.db.conn.execute(
            "UPDATE snapshots SET state='published', revision=revision+1, "
            "published_at=? WHERE id=?", (utcnow(), snapshot_id))
    return {"snapshot_id": snapshot_id, "state": "published",
            "revision": base_rev + 1}


def get_snapshot(self, snapshot_id: int) -> dict[str, Any]:
    row = self.db.one("SELECT * FROM snapshots WHERE id=?", (snapshot_id,))
    if row is None:
        raise NotFound(f"snapshot {snapshot_id} not found")
    return {"id": row["id"], "label": row["label"], "state": row["state"],
            "revision": row["revision"],
            "payload_fingerprint": row["payload_fingerprint"],
            "parser_version": row["parser_version"],
            "created_at": row["created_at"],
            "published_at": row["published_at"],
            "body": json.loads(row["body"])}


def list_snapshots(self) -> list[dict[str, Any]]:
    rows = self.db.query(
        "SELECT id,label,state,revision,payload_fingerprint,created_at,"
        "published_at FROM snapshots ORDER BY id")
    return [dict(r) for r in rows]
