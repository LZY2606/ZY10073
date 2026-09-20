"""Researcher decisions and triggered re-resolution."""
from __future__ import annotations

import json
from typing import Any

from ..db import utcnow
from ..models import Conflict, NotFound, ValidationError
from .context import load_citations, load_context
from ..resolver import resolve


def _affected_citation_ids(db, citation, payload: dict[str, Any]) -> set[int]:
    """Every citation whose answer could change after this decision.

    * relative citations (前款/前条/本章/本条) inside the same version are
      re-evaluated because their offsets/context may depend on the chosen
      article;
    * explicit citations with a candidate intersecting the decided target
      version+unit are re-evaluated (their ranking may change);
    * the decided citation itself, always.
    """
    affected = {citation["id"]}
    target_version = payload.get("target_version_id")
    target_unit = payload.get("target_unit_id")
    all_cites = db.query("SELECT id, version_id, kind FROM citations")
    for row in all_cites:
        if row["kind"] in ("prev_paragraph", "prev_article",
                           "this_chapter", "this_article") \
                and row["version_id"] == citation["version_id"]:
            affected.add(row["id"])
    if target_version:
        cand_rows = db.query(
            "SELECT citation_id FROM candidates WHERE target_version_id=?",
            (target_version,))
        for row in cand_rows:
            if target_unit is None:
                affected.add(row["citation_id"])
            else:
                hit = db.one(
                    "SELECT 1 FROM candidates WHERE citation_id=? "
                    "AND target_version_id=? AND ("
                    "target_unit_id=? OR target_unit_id IS NULL)",
                    (row["citation_id"], target_version, target_unit))
                if hit:
                    affected.add(row["citation_id"])
    return affected


def apply_decision(self, *, citation_id: int, researcher: str, action: str,
                   base_rev: int, target_version_id: int | None = None,
                   target_unit_id: int | None = None,
                   valid_from: str | None = None, valid_to: str | None = None,
                   note: str = "", source_page: str = "") -> dict[str, Any]:
    if action not in ("override", "version_range", "clear"):
        raise ValidationError("action must be override|version_range|clear")
    if valid_from and valid_to and not (valid_from < valid_to):
        raise ValidationError("valid_from must be earlier than valid_to")

    with self.db.lock:
        citation = self.db.one(
            "SELECT * FROM citations WHERE id=?", (citation_id,))
        if citation is None:
            raise NotFound(f"citation {citation_id} not found")
        if citation["decision_rev"] != base_rev:
            prior = self.db.one(
                "SELECT * FROM decisions WHERE citation_id=? "
                "ORDER BY id DESC LIMIT 1", (citation_id,))
            raise Conflict(
                "citation decisions changed since your read; "
                "both sides are returned for manual reconciliation",
                base={"rev": base_rev, "researcher": researcher},
                current=None if prior is None else {
                    "rev": citation["decision_rev"],
                    "researcher": prior["researcher"],
                    "target_version_id": prior["target_version_id"],
                    "target_unit_id": prior["target_unit_id"],
                    "valid_from": prior["valid_from"],
                    "valid_to": prior["valid_to"],
                    "note": prior["note"],
                    "source_page": prior["source_page"]})

        self.db.tx()
        try:
            if action == "clear":
                self.db.conn.execute(
                    "DELETE FROM decisions WHERE citation_id=? AND researcher=?",
                    (citation_id, researcher))
            else:
                self.db.conn.execute(
                    "INSERT INTO decisions(citation_id,researcher,action,"
                    "target_version_id,target_unit_id,valid_from,valid_to,"
                    "note,source_page,base_rev,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(citation_id,researcher) DO UPDATE SET "
                    "action=excluded.action,target_version_id=excluded.target_version_id,"
                    "target_unit_id=excluded.target_unit_id,"
                    "valid_from=excluded.valid_from,valid_to=excluded.valid_to,"
                    "note=excluded.note,source_page=excluded.source_page,"
                    "base_rev=excluded.base_rev,created_at=excluded.created_at",
                    (citation_id, researcher, action, target_version_id,
                     target_unit_id, valid_from, valid_to, note, source_page,
                     base_rev, utcnow()))
            self.db.conn.execute(
                "UPDATE citations SET decision_rev=decision_rev+1 WHERE id=?",
                (citation_id,))
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise

        affected = _affected_citation_ids(self.db, citation, {
            "target_version_id": target_version_id,
            "target_unit_id": target_unit_id})

        hints: dict[int, dict[str, Any]] = {}
        if action != "clear" and target_version_id is not None:
            hints[citation_id] = {
                "target_version_id": target_version_id,
                "target_unit_id": target_unit_id,
                "researcher": researcher,
                "valid_from": valid_from,
                "valid_to": valid_to}
        self.reresolve_all(only=affected, hints=hints)
        new_rev = citation["decision_rev"] + 1

    return {"citation_id": citation_id, "decision_rev": new_rev,
            "reparsed": sorted(affected)}
