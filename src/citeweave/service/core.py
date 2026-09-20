"""CiteWeave service: business rules with persistence-driven state."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

from .. import PARSER_VERSION
from ..db import DB, utcnow
from ..models import (Conflict, NotFound, StagedError, ValidationError,
                      VersionState, check_transition,
                      VERSION_TRANSITIONS, LEGAL_SNAPSHOT_TRANSITIONS)
from ..parser import parse_document
from ..signing import citation_signature
from ..resolver import VCitation, resolve
from ..textutils import content_fingerprint, raw_fingerprint
from .context import load_citations, load_context
from .decisions import apply_decision
from .exporter import build_bundle, determinism_manifest, verify_anchors
from .graph import date_diff, graph_at
from .merger import create_merge_session, get_merge_session, merge_session
from .snapshots import create_snapshot, get_snapshot, list_snapshots, publish_snapshot, snapshot_payload

CRASH_POINT_AFTER_STAGE = "CITEWEAVE_CRASH_AFTER_STAGE"


class CiteWeaveService:
    def __init__(self, db: DB):
        self.db = db

    # ================= documents & imports =================
    def import_version(self, *, code: str, title: str, label: str,
                       raw_text: str, effective_date: str,
                       adopted_date: str | None = None,
                       end_date: str | None = None,
                       state: str = VersionState.ADOPTED.value,
                       idempotency_key: str | None = None,
                       stage: str = "final") -> dict[str, Any]:
        """Import one document version verbatim, then parse + resolve.

        ``stage='staged'`` only writes the raw version row; ``finalize``
        runs parsing and resolution.  Separating the two lets tests kill
        the process between them and verify recovery.
        """
        if stage not in ("staged", "final"):
            raise ValidationError("stage must be staged|final")
        request_key = None
        if idempotency_key:
            request_key = f"import:{idempotency_key}"
            cached = self.db.cached_request(request_key)
            if cached is not None:
                return {"replayed": True, "status": cached[0], **cached[1]}

        with self.db.lock:
            existing = self.db.one(
                "SELECT v.id, raw_fingerprint FROM versions v "
                "JOIN documents d ON d.id=v.document_id "
                "WHERE d.code=? AND v.version_label=?", (code, label))
            if existing is not None:
                if existing["raw_fingerprint"] == raw_fingerprint(raw_text):
                    return {"replayed": True, "status": 200,
                            "version_id": existing["id"]}
                raise Conflict(
                    f"version {label} of {code} already exists with different "
                    "text; normalization/edits must be saved as a new version",
                    base=existing["raw_fingerprint"],
                    current=raw_fingerprint(raw_text))

            self.db.tx()
            try:
                now = utcnow()
                doc = self.db.one(
                    "SELECT id FROM documents WHERE code=?", (code,))
                if doc is None:
                    cur = self.db.conn.execute(
                        "INSERT INTO documents(code,title,created_at,updated_at)"
                        " VALUES(?,?,?,?)", (code, title, now, now))
                    doc_id = cur.lastrowid
                else:  # noqa: E501
                    doc_id = doc["id"]
                    self.db.conn.execute(
                        "UPDATE documents SET title=?, updated_at=? WHERE id=?",
                        (title, now, doc_id))
                cur = self.db.conn.execute(
                    "INSERT INTO versions(document_id,version_label,state,"
                    "adopted_date,effective_date,end_date,title,raw_text,"
                    "raw_fingerprint,norm_fingerprint,parser_version,"
                    "import_stage,staged_at,idempotency_key,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (doc_id, label, state, adopted_date, effective_date,
                     end_date, title, raw_text, raw_fingerprint(raw_text),
                     content_fingerprint(raw_text), PARSER_VERSION,
                     stage, now if stage == "staged" else None,
                     idempotency_key, now))
                version_id = cur.lastrowid
                if stage == "staged":
                    self.db.commit()
                    if os.environ.get(CRASH_POINT_AFTER_STAGE) == str(version_id):
                        # Simulate SIGKILL after durable commit: force the WAL
                        # onto disk first, then terminate without cleanup.
                        self.db.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                        os._exit(17)
                    result = {"status": 202, "version_id": version_id,
                              "stage": "staged"}
                    if request_key:
                        self.db.remember_request(request_key, 202, result)
                    return result
                self._populate_parsed(version_id, raw_text, code)
                self.db.commit()
            except Exception:
                self.db.rollback()
                raise

        self.reresolve_all()
        self._autosupersede_later()
        result = {"status": 201, "version_id": version_id, "stage": "final"}
        if request_key:
            with self.db.lock:
                self.db.remember_request(request_key, 201, result)
        return result

    def finalize_version(self, version_id: int) -> dict[str, Any]:
        with self.db.lock:
            row = self.db.one(
                "SELECT v.*, d.code AS doc_code FROM versions v "
                "JOIN documents d ON d.id=v.document_id WHERE v.id=?",
                (version_id,))
            if row is None:
                raise NotFound(f"version {version_id} not found")
            if row["import_stage"] == "final":
                return {"status": 200, "version_id": version_id,
                        "stage": "final", "replayed": True}
            if row["import_stage"] == "abandoned":
                raise Conflict(
                    "staged import was abandoned by crash recovery; "
                    "re-import to proceed")
            self.db.tx()
            try:
                self._populate_parsed(version_id, row["raw_text"],
                                      row["doc_code"])
                self.db.conn.execute(
                    "UPDATE versions SET import_stage='final', staged_at=NULL "
                    "WHERE id=?", (version_id,))
                self.db.commit()
            except Exception:
                self.db.rollback()
                raise
        self.reresolve_all()
        self._autosupersede_later()
        return {"status": 201, "version_id": version_id, "stage": "final"}

    def _populate_parsed(self, version_id: int, raw_text: str,
                             code: str) -> None:
        parsed = parse_document(raw_text)
        self.db.conn.execute(
            "UPDATE versions SET article_count=? WHERE id=?",
            (parsed.article_count, version_id))
        for unit in parsed.units:
            self.db.conn.execute(
                "INSERT INTO units(version_id,key,kind,num,title,start_offset,"
                "end_offset,level,ordinal,article_num,path) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (version_id, unit.key, unit.kind, unit.num, unit.title,
                 unit.start, unit.end, unit.level, unit.ordinal,
                 unit.article_num, json.dumps(list(unit.path))))
        for citation in parsed.citations:
            signature = citation_signature(
                code_label=code, article_num=citation.article_num,
                kind=citation.kind, text=citation.text,
                descriptor=citation.descriptor)
            cur = self.db.conn.execute(
                "INSERT INTO citations(version_id,source_article_num,"
                "paragraph_idx,kind,text,start_offset,end_offset,sentence,"
                "sent_start,signature,descriptor) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (version_id, citation.article_num, citation.paragraph_idx,
                 citation.kind, citation.text, citation.start, citation.end,
                 citation.sentence, citation.sent_start, signature,
                 json.dumps(citation.descriptor, ensure_ascii=False)))

    def _autosupersede_later(self) -> None:
        """Mark prior effective versions superseded where windows overlap."""
        with self.db.lock:
            rows = self.db.query(
                "SELECT id, document_id, effective_date FROM versions "
                "WHERE state='effective' AND import_stage='final' "
                "ORDER BY document_id, effective_date, id")
            by_doc: dict[int, list] = {}
            for row in rows:
                by_doc.setdefault(row["document_id"], []).append(row)
            self.db.tx()
            try:
                for doc_id, group in by_doc.items():
                    latest = group[-1]
                    for row in group[:-1]:
                        end_date = latest["effective_date"]
                        if row["effective_date"] < end_date:
                            self.db.conn.execute(
                                "UPDATE versions SET state='superseded',"
                                "end_date=? WHERE id=? AND state='effective'",
                                (end_date, row["id"]))
                self.db.commit()
            except Exception:
                self.db.rollback()
                raise

    def transition_version(self, version_id: int, to_state: str) -> dict[str, Any]:
        with self.db.lock:
            row = self.db.get("versions", version_id)
            check_transition(VERSION_TRANSITIONS, row["state"], to_state)
            if row["import_stage"] != "final":
                raise Conflict("cannot transition a non-final import")
            self.db.tx()
            try:
                updates = ["state=?"]
                params: list[Any] = [to_state]
                if to_state == VersionState.EFFECTIVE.value:
                    updates.append("effective_date=COALESCE(effective_date,?)")
                    params.append(utcnow()[:10])
                if to_state == VersionState.SUPERSEDED.value:
                    updates.append("end_date=?")
                    params.append(utcnow()[:10])
                elif to_state == VersionState.REPEALED.value:
                    # A superseded version already has an end_date; keep it.
                    updates.append("end_date=COALESCE(end_date,?)")
                    params.append(utcnow()[:10])
                params.append(version_id)
                self.db.conn.execute(
                    f"UPDATE versions SET {', '.join(updates)} WHERE id=?",
                    params)
                self.db.commit()
            except Exception:
                self.db.rollback()
                raise
        self.reresolve_all()
        return {"version_id": version_id, "state": to_state}

    # ================= resolution =================
    def reresolve_all(self, only: set[int] | None = None,
                      hints: dict[int, dict[str, Any]] | None = None) -> int:
        """Re-run deterministic resolution; ``only`` = affected citation ids."""
        hints = hints or {}
        with self.db.lock:
            versions, docs = load_context(self.db)
            citations = load_citations(self.db)
            by_id = {v.version_id: v for v in versions}
            count = 0
            self.db.tx()
            try:
                for citation in citations:
                    if only is not None and citation.citation_id not in only:
                        continue
                    source = by_id.get(citation.version_id)
                    if source is None:
                        continue
                    candidates, status, basis = resolve(
                        citation, source, versions, docs,
                        hints.get(citation.citation_id))
                    self._write_resolution(citation.citation_id, candidates,
                                           status, basis)
                    count += 1
                self.db.commit()
            except Exception:
                self.db.rollback()
                raise
        return count

    def _write_resolution(self, citation_id: int, candidates, status: str,
                          basis: dict[str, Any]) -> None:
        self.db.conn.execute(
            "UPDATE resolutions SET selected_candidate_id=NULL "
            "WHERE citation_id=?", (citation_id,))
        self.db.conn.execute(
            "DELETE FROM candidates WHERE citation_id=?", (citation_id,))
        winner_id = None
        ids: list[int] = []
        for rank, candidate in enumerate(candidates):
            cur = self.db.conn.execute(
                "INSERT INTO candidates(citation_id,rank,score,"
                "target_version_id,target_unit_id,target_desc,status,basis) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (citation_id, rank, candidate.score,
                 candidate.target_version_id, candidate.target_unit_id,
                 candidate.target_desc, candidate.status,
                 json.dumps(candidate.basis, ensure_ascii=False)))
            ids.append(cur.lastrowid)
        if status in ("resolved", "repealed", "ambiguous",
                      "cross_version_conflict"):
            real = [c for c in candidates if c.target_unit_id is not None]
            if real and status != "missing":
                top_rank = candidates.index(real[0])
                winner_id = ids[top_rank]
        existing = self.db.one(
            "SELECT id, revision FROM resolutions WHERE citation_id=?",
            (citation_id,))
        if existing is None:
            self.db.conn.execute(
                "INSERT INTO resolutions(citation_id,selected_candidate_id,"
                "status,basis,parser_version,resolved_at,revision) "
                "VALUES(?,?,?,?,?,?,1)",
                (citation_id, winner_id, status,
                 json.dumps(basis, ensure_ascii=False),
                 PARSER_VERSION, utcnow()))
        else:
            self.db.conn.execute(
                "UPDATE resolutions SET selected_candidate_id=?, status=?,"
                "basis=?, parser_version=?, resolved_at=?, revision=revision+1 "
                "WHERE id=?",
                (winner_id, status, json.dumps(basis, ensure_ascii=False),
                 PARSER_VERSION, utcnow(), existing["id"]))

    # decisions / graph / snapshots / merges / exports are mixed in below.
    add_decision = apply_decision
    graph_at = graph_at
    date_diff = date_diff
    create_snapshot = create_snapshot
    publish_snapshot = publish_snapshot
    get_snapshot = get_snapshot
    list_snapshots = list_snapshots
    snapshot_payload = snapshot_payload
    create_merge_session = create_merge_session
    merge_session = merge_session
    get_merge_session = get_merge_session
    build_bundle = build_bundle
    determinism_manifest = staticmethod(determinism_manifest)
    verify_anchors = verify_anchors

    # ================= reads =================
    def list_documents(self) -> list[dict[str, Any]]:
        rows = self.db.query(
            "SELECT d.id,d.code,d.title,d.updated_at,"
            "COUNT(v.id) AS version_count FROM documents d "
            "LEFT JOIN versions v ON v.document_id=d.id "
            "AND v.import_stage='final' "
            "GROUP BY d.id ORDER BY d.code")
        return [dict(r) for r in rows]

    def get_version(self, version_id: int) -> dict[str, Any]:
        row = self.db.one(
            "SELECT v.*, d.code FROM versions v JOIN documents d "
            "ON d.id=v.document_id WHERE v.id=?", (version_id,))
        if row is None:
            raise NotFound(f"version {version_id} not found")
        return dict(row)

    def list_versions(self, code: str | None = None) -> list[dict[str, Any]]:
        if code:
            rows = self.db.query(
                "SELECT v.*, d.code FROM versions v JOIN documents d "
                "ON d.id=v.document_id WHERE d.code=? "
                "ORDER BY v.effective_date, v.id", (code,))
        else:
            rows = self.db.query(
                "SELECT v.*, d.code FROM versions v JOIN documents d "
                "ON d.id=v.document_id ORDER BY d.code, v.effective_date, v.id")
        return [dict(r) for r in rows]

    def citation_detail(self, citation_id: int) -> dict[str, Any]:
        cite = self.db.one(
            "SELECT c.*, d.code AS doc_code FROM citations c "
            "JOIN versions v ON v.id=c.version_id "
            "JOIN documents d ON d.id=v.document_id WHERE c.id=?",
            (citation_id,))
        if cite is None:
            raise NotFound(f"citation {citation_id} not found")
        candidates = self.db.query(
            "SELECT * FROM candidates WHERE citation_id=? ORDER BY rank",
            (citation_id,))
        resolution = self.db.one(
            "SELECT * FROM resolutions WHERE citation_id=?", (citation_id,))
        decisions = self.db.query(
            "SELECT * FROM decisions WHERE citation_id=? ORDER BY id",
            (citation_id,))
        return {
            "citation": {**dict(cite),
                         "descriptor": json.loads(cite["descriptor"])},
            "candidates": [dict(c) | {"basis": json.loads(c["basis"])}
                           for c in candidates],
            "resolution": (dict(resolution)
                           | {"basis": json.loads(resolution["basis"])}
                           if resolution else None),
            "decisions": [dict(d) for d in decisions],
        }

    def recovery_status(self) -> dict[str, Any]:
        abandoned = self.db.query(
            "SELECT id, version_label, title FROM versions "
            "WHERE import_stage='abandoned' ORDER BY id")
        return {"abandoned_stages": [dict(r) for r in abandoned]}
