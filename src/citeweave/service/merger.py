"""Two-researcher merge: keep both contexts, never last-write-wins."""
from __future__ import annotations

from typing import Any

from ..db import utcnow
from ..models import Conflict, NotFound, ValidationError


def _decision_context(db, decision) -> dict[str, Any]:
    cite = db.one("SELECT * FROM citations WHERE id=?",
                  (decision["citation_id"],))
    quote, context = "", ""
    if cite is not None:
        quote = cite["text"]
        start = max(0, cite["start_offset"] - 24)
        end = cite["end_offset"] + 24
        version = db.one("SELECT raw_text FROM versions WHERE id=?",
                         (cite["version_id"],))
        if version is not None:
            context = version["raw_text"][start:end]
    return {
        "citation_id": decision["citation_id"],
        "quote": quote,
        "context": context,
        "target_version_id": decision["target_version_id"],
        "target_unit_id": decision["target_unit_id"],
        "valid_from": decision["valid_from"],
        "valid_to": decision["valid_to"],
        "note": decision["note"],
        "source_page": decision["source_page"],
        "action": decision["action"],
    }


def create_merge_session(self, *, label: str, base_version_id: int,
                         researcher_a: str, researcher_b: str) -> dict[str, Any]:
    if researcher_a == researcher_b:
        raise ValidationError("merge requires two distinct researchers")
    base = self.db.get("versions", base_version_id)
    rows_a = self.db.query(
        "SELECT * FROM decisions WHERE researcher=? AND citation_id IN "
        "(SELECT id FROM citations WHERE version_id=?)",
        (researcher_a, base_version_id))
    rows_b = self.db.query(
        "SELECT * FROM decisions WHERE researcher=? AND citation_id IN "
        "(SELECT id FROM citations WHERE version_id=?)",
        (researcher_b, base_version_id))
    by_a = {r["citation_id"]: r for r in rows_a}
    by_b = {r["citation_id"]: r for r in rows_b}
    all_ids = sorted(set(by_a) | set(by_b))

    agreements, divergences = [], []
    session_id: int
    with self.db.lock:
        cur = self.db.conn.execute(
            "INSERT INTO merge_sessions(label,state,base_version_id,"
            "researcher_a,researcher_b,revision,created_at) "
            "VALUES(?,?,?,?,?,1,?)",
            (label, "open", base_version_id, researcher_a, researcher_b,
             utcnow()))
        session_id = cur.lastrowid
        for cid in all_ids:
            a, b = by_a.get(cid), by_b.get(cid)
            same_target = (
                a and b and a["action"] == b["action"]
                and a["target_version_id"] == b["target_version_id"]
                and a["target_unit_id"] == b["target_unit_id"]
                and a["valid_from"] == b["valid_from"]
                and a["valid_to"] == b["valid_to"])
            bucket = agreements if same_target else divergences
            bucket.append(cid)
            for side, decision in (("A", a), ("B", b)):
                if decision is None:
                    continue
                ctx = _decision_context(self.db, decision)
                self.db.conn.execute(
                    "INSERT INTO merge_items(session_id,citation_id,side,"
                    "researcher,target_version_id,target_unit_id,valid_from,"
                    "valid_to,note,source_page,quote,context) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (session_id, cid, side, decision["researcher"],
                     decision["target_version_id"],
                     decision["target_unit_id"], decision["valid_from"],
                     decision["valid_to"], decision["note"],
                     decision["source_page"], ctx["quote"], ctx["context"]))
    return {"session_id": session_id, "state": "open",
            "agreements": agreements, "divergences": divergences,
            "researcher_a": researcher_a, "researcher_b": researcher_b}


def merge_session(self, session_id: int, base_rev: int,
                  resolutions: dict[str, str] | None = None) -> dict[str, Any]:
    """Close a session.

    Divergences stay preserved; ``resolutions`` maps citation ids (str) to
    ``A`` / ``B`` for the cases a human adjudicates.  Nothing is silently
    overwritten -- unresolved divergences remain listed.
    """
    resolutions = resolutions or {}
    with self.db.lock:
        row = self.db.one("SELECT * FROM merge_sessions WHERE id=?",
                          (session_id,))
        if row is None:
            raise NotFound(f"merge session {session_id} not found")
        if row["state"] == "merged":
            items = self.db.query(
                "SELECT DISTINCT citation_id FROM merge_items WHERE session_id=?",
                (session_id,))
            return {"session_id": session_id, "state": "merged",
                    "replayed": True,
                    "adjudicated": [r["citation_id"] for r in items],
                    "divergences_kept": []}
        if row["revision"] != base_rev:
            raise Conflict("merge session revision is stale",
                           base=base_rev, current=row["revision"])
        items = self.db.query(
            "SELECT * FROM merge_items WHERE session_id=? ORDER BY citation_id,"
            " side", (session_id,))
        by_cite: dict[int, list] = {}
        for item in items:
            by_cite.setdefault(item["citation_id"], []).append(item)
        adjudicated, still_open = [], []
        for cid, sides in by_cite.items():
            if len(sides) == 1:
                adjudicated.append(cid)
                continue
            a, b = sides
            same = (a["target_version_id"] == b["target_version_id"]
                    and a["target_unit_id"] == b["target_unit_id"]
                    and a["valid_from"] == b["valid_from"]
                    and a["valid_to"] == b["valid_to"])
            if same:
                adjudicated.append(cid)
                continue
            choice = resolutions.get(str(cid))
            if choice in ("A", "B"):
                adjudicated.append(cid)
            else:
                still_open.append(cid)
        self.db.conn.execute(
            "UPDATE merge_sessions SET state='merged', revision=revision+1,"
            "merged_at=? WHERE id=?", (utcnow(), session_id))
    return {"session_id": session_id, "state": "merged",
            "adjudicated": adjudicated, "divergences_kept": still_open}


def get_merge_session(self, session_id: int) -> dict[str, Any]:
    row = self.db.one("SELECT * FROM merge_sessions WHERE id=?",
                      (session_id,))
    if row is None:
        raise NotFound(f"merge session {session_id} not found")
    items = self.db.query(
        "SELECT * FROM merge_items WHERE session_id=? ORDER BY citation_id,side",
        (session_id,))
    grouped: dict[int, list[dict[str, Any]]] = {}
    for item in items:
        grouped.setdefault(item["citation_id"], []).append(dict(item))
    divergences = [cid for cid, sides in grouped.items()
                   if len(sides) > 2 or (
                       len(sides) == 2 and (
                           sides[0]["target_version_id"]
                           != sides[1]["target_version_id"]
                           or sides[0]["target_unit_id"]
                           != sides[1]["target_unit_id"]
                           or sides[0]["valid_from"]
                           != sides[1]["valid_from"]
                           or sides[0]["valid_to"]
                           != sides[1]["valid_to"]))]
    return {**dict(row), "items": grouped, "divergences": divergences}
