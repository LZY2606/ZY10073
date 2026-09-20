from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from .parser import PARSER_VERSION, fingerprint, normalize_text, parse_document


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class ConflictError(Exception):
    def __init__(self, message: str, difference: dict[str, Any]):
        super().__init__(message)
        self.difference = difference


class IllegalTransitionError(ValueError):
    pass


class NotFoundError(LookupError):
    pass


SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS documents (
    id TEXT PRIMARY KEY,
    doc_key TEXT NOT NULL,
    version_label TEXT NOT NULL,
    title TEXT NOT NULL,
    raw_text TEXT NOT NULL,
    normalized_text TEXT NOT NULL,
    source_fingerprint TEXT NOT NULL,
    parser_version TEXT NOT NULL,
    status TEXT NOT NULL,
    effective_date TEXT,
    repealed_date TEXT,
    lock_version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(doc_key, version_label)
);
CREATE TABLE IF NOT EXISTS document_transitions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id TEXT NOT NULL REFERENCES documents(id),
    from_status TEXT,
    to_status TEXT NOT NULL,
    changed_at TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS clauses (
    id TEXT PRIMARY KEY,
    document_id TEXT NOT NULL REFERENCES documents(id),
    semantic_key TEXT NOT NULL,
    locator TEXT NOT NULL,
    level TEXT NOT NULL,
    number TEXT,
    number_value INTEGER,
    heading TEXT NOT NULL,
    text TEXT NOT NULL,
    normalized_text TEXT NOT NULL,
    parent_semantic_key TEXT,
    chapter_semantic_key TEXT,
    article_semantic_key TEXT,
    previous_paragraph_semantic_key TEXT,
    ordinal INTEGER NOT NULL,
    text_fingerprint TEXT NOT NULL,
    normalized_fingerprint TEXT NOT NULL,
    UNIQUE(document_id, semantic_key)
);
CREATE TABLE IF NOT EXISTS citations (
    id TEXT PRIMARY KEY,
    document_id TEXT NOT NULL REFERENCES documents(id),
    semantic_key TEXT NOT NULL,
    source_clause_id TEXT NOT NULL REFERENCES clauses(id),
    source_semantic_key TEXT NOT NULL,
    sentence TEXT NOT NULL,
    quote TEXT NOT NULL,
    ref_type TEXT NOT NULL,
    requested_article INTEGER,
    requested_paragraph INTEGER,
    requested_item INTEGER,
    char_start INTEGER NOT NULL,
    char_end INTEGER NOT NULL,
    line_number INTEGER NOT NULL,
    anchor_locator TEXT NOT NULL,
    anchor_fingerprint TEXT NOT NULL,
    normalized_anchor_fingerprint TEXT NOT NULL,
    selected_candidate_id TEXT,
    resolution_status TEXT NOT NULL DEFAULT 'pending',
    resolution_reason TEXT NOT NULL DEFAULT '',
    lock_version INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL,
    UNIQUE(document_id, semantic_key)
);
CREATE TABLE IF NOT EXISTS candidates (
    id TEXT PRIMARY KEY,
    citation_id TEXT NOT NULL REFERENCES citations(id),
    target_clause_id TEXT NOT NULL REFERENCES clauses(id),
    ordinal INTEGER NOT NULL,
    score INTEGER NOT NULL,
    reason TEXT NOT NULL,
    exact INTEGER NOT NULL DEFAULT 0,
    UNIQUE(citation_id, target_clause_id)
);
CREATE TABLE IF NOT EXISTS decisions (
    id TEXT PRIMARY KEY,
    citation_semantic_key TEXT NOT NULL,
    source_citation_id TEXT NOT NULL,
    source_version_label TEXT NOT NULL,
    source_locator TEXT NOT NULL,
    original_sentence TEXT NOT NULL,
    quote TEXT NOT NULL,
    researcher TEXT NOT NULL,
    target_clause_id TEXT,
    target_semantic_key TEXT,
    rationale TEXT NOT NULL DEFAULT '',
    applies_from_version TEXT,
    applies_to_version TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL,
    revoked_at TEXT,
    merge_parent_id TEXT REFERENCES decisions(id),
    lock_version INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS decisions_active_idx
    ON decisions(citation_semantic_key, status, applies_from_version, applies_to_version);
CREATE TABLE IF NOT EXISTS snapshots (
    id TEXT PRIMARY KEY,
    label TEXT NOT NULL UNIQUE,
    at_date TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    package_fingerprint TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS idempotency_keys (
    key TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    response_code INTEGER NOT NULL,
    response_body TEXT NOT NULL
);
"""


class Store:
    def __init__(self, path: str | Path = ":memory:"):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.write_lock = threading.RLock()
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        if self.path != ":memory:":
            self.conn.execute("PRAGMA journal_mode = WAL")
            self.conn.execute("PRAGMA synchronous = FULL")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def transaction(self):
        try:
            yield self.conn
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def _id(self, prefix: str, *parts: Any) -> str:
        return f"{prefix}_{hashlib.sha256(canonical_json(list(parts)).encode()).hexdigest()[:16]}"

    def idempotent_get(self, key: str | None) -> dict[str, Any] | None:
        if not key:
            return None
        row = self.conn.execute(
            "SELECT response_code, response_body FROM idempotency_keys WHERE key = ?", (key,)
        ).fetchone()
        if not row:
            return None
        return {"code": row["response_code"], "body": json.loads(row["response_body"])}

    def idempotent_put(self, key: str | None, code: int, body: dict[str, Any]) -> None:
        if not key:
            return
        with self.write_lock, self.transaction() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO idempotency_keys(key, created_at, response_code, response_body) VALUES (?, ?, ?, ?)",
                (key, utc_now(), code, canonical_json(body)),
            )

    def import_document(
        self,
        *,
        doc_key: str,
        version_label: str,
        title: str | None,
        raw_text: str,
        effective_date: str | None,
        repealed_date: str | None,
        status: str = "ready",
    ) -> dict[str, Any]:
        if not doc_key or not version_label or not raw_text.strip():
            raise ValueError("doc_key, version_label and raw_text are required")
        if status not in {"ready", "active"}:
            raise ValueError("initial status must be ready or active")
        document_id = self._id("doc", doc_key, version_label)
        now = utc_now()
        existing = self.conn.execute("SELECT * FROM documents WHERE id = ?", (document_id,)).fetchone()
        with self.write_lock, self.transaction() as conn:
            existing = conn.execute("SELECT * FROM documents WHERE id = ?", (document_id,)).fetchone()
            if existing:
                duplicate_result = {"document": self._document(existing["id"]), "duplicate": True}
                committed_duplicate = True
            else:
                committed_duplicate = False
                parsed = parse_document(
                    raw_text,
                    doc_key=doc_key,
                    version_label=version_label,
                )
                resolved_title = title or doc_key
                conn.execute(
                    """INSERT INTO documents(id, doc_key, version_label, title, raw_text, normalized_text,
                       source_fingerprint, parser_version, status, effective_date, repealed_date,
                       lock_version, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)""",
                    (
                        document_id,
                        doc_key,
                        version_label,
                        resolved_title,
                        parsed.raw_text,
                        parsed.normalized_text,
                        fingerprint(parsed.raw_text),
                        PARSER_VERSION,
                        status,
                        effective_date,
                        repealed_date,
                        now,
                        now,
                    ),
                )
                conn.execute(
                    "INSERT INTO document_transitions(document_id, from_status, to_status, changed_at, reason) VALUES (?, NULL, ?, ?, ?)",
                    (document_id, status, now, "imported"),
                )
                for clause in parsed.clauses:
                    conn.execute(
                        """INSERT INTO clauses(id, document_id, semantic_key, locator, level, number, number_value,
                           heading, text, normalized_text, parent_semantic_key, chapter_semantic_key,
                           article_semantic_key, previous_paragraph_semantic_key, ordinal, text_fingerprint,
                           normalized_fingerprint) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            clause["id"],
                            document_id,
                            clause["semantic_key"],
                            clause["locator"],
                            clause["level"],
                            clause["number"],
                            clause["number_value"],
                            clause["heading"],
                            clause["text"],
                            clause["normalized_text"],
                            clause["parent_semantic_key"],
                            clause["chapter_semantic_key"],
                            clause["article_semantic_key"],
                            clause["previous_paragraph_semantic_key"],
                            clause["ordinal"],
                            clause["text_fingerprint"],
                            clause["normalized_fingerprint"],
                        ),
                    )
                for citation in parsed.citations:
                    conn.execute(
                        """INSERT INTO citations(id, document_id, semantic_key, source_clause_id, source_semantic_key,
                           sentence, quote, ref_type, requested_article, requested_paragraph, requested_item,
                           char_start, char_end, line_number, anchor_locator, anchor_fingerprint,
                           normalized_anchor_fingerprint, selected_candidate_id, resolution_status,
                           resolution_reason, lock_version, updated_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, 'pending', '', 1, ?)""",
                        (
                            citation["id"],
                            document_id,
                            citation["semantic_key"],
                            citation["source_clause_id"],
                            citation["source_semantic_key"],
                            citation["sentence"],
                            citation["quote"],
                            citation["ref_type"],
                            citation["requested_article"],
                            citation["requested_paragraph"],
                            citation["requested_item"],
                            citation["char_start"],
                            citation["char_end"],
                            citation["line_number"],
                            citation["anchor_locator"],
                            citation["anchor_fingerprint"],
                            citation["normalized_anchor_fingerprint"],
                            now,
                        ),
                    )
        if committed_duplicate:
            return duplicate_result
        self.resolve_all_citations()
        return {"document": self._document(document_id), "duplicate": False}

    def _row(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Row:
        row = self.conn.execute(sql, params).fetchone()
        if row is None:
            raise NotFoundError()
        return row

    def _document(self, document_id: str) -> dict[str, Any]:
        row = self._row("SELECT * FROM documents WHERE id = ?", (document_id,))
        return dict(row)

    def get_document_by_version(self, doc_key: str, version_label: str) -> dict[str, Any]:
        return dict(
            self._row(
                "SELECT * FROM documents WHERE doc_key = ? AND version_label = ?",
                (doc_key, version_label),
            )
        )

    def list_documents(self) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in self.conn.execute(
                "SELECT * FROM documents ORDER BY doc_key, version_label, created_at"
            ).fetchall()
        ]

    def get_document_detail(self, document_id: str) -> dict[str, Any]:
        document = self._document(document_id)
        clauses = [
            dict(row)
            for row in self.conn.execute(
                "SELECT * FROM clauses WHERE document_id = ? ORDER BY ordinal", (document_id,)
            ).fetchall()
        ]
        citations = [
            self.get_citation(row["id"])
            for row in self.conn.execute(
                "SELECT id FROM citations WHERE document_id = ? ORDER BY char_start, id", (document_id,)
            ).fetchall()
        ]
        return {**document, "clauses": clauses, "citations": citations}

    def _clause(self, clause_id: str) -> dict[str, Any]:
        return dict(self._row("SELECT * FROM clauses WHERE id = ?", (clause_id,)))

    def get_citation(self, citation_id: str) -> dict[str, Any]:
        row = self._row("SELECT * FROM citations WHERE id = ?", (citation_id,))
        candidates = [
            dict(candidate)
            for candidate in self.conn.execute(
                """SELECT c.*, cl.document_id AS target_document_id, cl.semantic_key AS target_semantic_key,
                          cl.locator AS target_locator, cl.text AS target_text, d.version_label AS target_version_label,
                          d.status AS target_document_status, d.effective_date AS target_effective_date,
                          d.repealed_date AS target_repealed_date
                   FROM candidates c JOIN clauses cl ON c.target_clause_id = cl.id
                   JOIN documents d ON cl.document_id = d.id
                   WHERE c.citation_id = ? ORDER BY c.ordinal""",
                (citation_id,),
            ).fetchall()
        ]
        decisions = [
            dict(decision)
            for decision in self.conn.execute(
                "SELECT * FROM decisions WHERE citation_semantic_key = ? ORDER BY created_at",
                (row["semantic_key"],),
            ).fetchall()
        ]
        base_citation = dict(row)
        base_citation["decisions"] = decisions
        alternative_targets = self._alternative_targets(base_citation)
        return {
            **base_citation,
            "candidates": candidates,
            "alternative_targets": alternative_targets,
        }

    def _alternative_targets(
        self, citation: dict[str, Any]
    ) -> list[dict[str, Any]]:
        alternatives = []
        for decision in citation.get("decisions", []):
            if decision["status"] != "active":
                continue
            target_row = self.conn.execute(
                "SELECT * FROM clauses WHERE document_id = ? AND semantic_key = ? ORDER BY ordinal, id LIMIT 1",
                (citation["document_id"], decision["target_semantic_key"]),
            ).fetchone()
            if not target_row:
                target_row = self.conn.execute(
                    "SELECT * FROM clauses WHERE id = ?", (decision["target_clause_id"],)
                ).fetchone()
            if not target_row:
                continue
            target_doc = self._document(target_row["document_id"])
            alternatives.append(
                {
                    "researcher": decision["researcher"],
                    "target_clause_id": target_row["id"],
                    "target_semantic_key": target_row["semantic_key"],
                    "target_locator": target_row["locator"],
                    "target_version_label": target_doc["version_label"],
                    "rationale": decision["rationale"],
                    "applies_from_version": decision["applies_from_version"],
                    "applies_to_version": decision["applies_to_version"],
                    "source_page": citation["anchor_locator"],
                    "original_sentence": citation["sentence"],
                }
            )
        return alternatives

    def list_citations(self) -> list[dict[str, Any]]:
        rows = self.conn.execute("SELECT id FROM citations ORDER BY id").fetchall()
        return [self.get_citation(row["id"]) for row in rows]

    def _possible_targets(self, citation: sqlite3.Row, source: sqlite3.Row) -> list[sqlite3.Row]:
        if citation["ref_type"] == "previous_paragraph":
            source_clause = self.conn.execute(
                "SELECT * FROM clauses WHERE id = ?", (citation["source_clause_id"],)
            ).fetchone()
            if not source_clause or not source_clause["previous_paragraph_semantic_key"]:
                return []
            rows = self.conn.execute(
                "SELECT cl.* FROM clauses cl WHERE cl.document_id = ? AND cl.semantic_key = ?",
                (citation["document_id"], source_clause["previous_paragraph_semantic_key"]),
            ).fetchall()
            return list(rows)
        if citation["ref_type"] == "current_chapter":
            source_clause = self.conn.execute(
                "SELECT * FROM clauses WHERE id = ?", (citation["source_clause_id"],)
            ).fetchone()
            if not source_clause or not source_clause["chapter_semantic_key"]:
                return []
            chapter_key = f"{source['doc_key']}|{source_clause['chapter_semantic_key']}"
            rows = self.conn.execute(
                """SELECT cl.* FROM clauses cl JOIN documents d ON cl.document_id = d.id
                   WHERE d.doc_key = ? AND cl.semantic_key = ? AND d.version_label = ?""",
                (source["doc_key"], chapter_key, source["version_label"]),
            ).fetchall()
            return list(rows)
        rows = self.conn.execute(
            """SELECT cl.* FROM clauses cl JOIN documents d ON cl.document_id = d.id
               WHERE d.doc_key = ? AND cl.number_value = ? AND cl.level = ?
               ORDER BY d.version_label, cl.ordinal""",
            (source["doc_key"], citation["requested_article"], "article"),
        ).fetchall()
        result: list[sqlite3.Row] = []
        for article in rows:
            if not citation["requested_paragraph"] and not citation["requested_item"]:
                result.append(article)
                continue
            wanted_level = "paragraph" if citation["requested_paragraph"] else "item"
            wanted_number = citation["requested_paragraph"] or citation["requested_item"]
            children = self.conn.execute(
                """SELECT * FROM clauses WHERE document_id = ? AND article_semantic_key = ?
                   AND level = ? AND number_value = ? ORDER BY ordinal""",
                (article["document_id"], article["number"], wanted_level, wanted_number),
            ).fetchall()
            result.extend(children)
        return result

    def _score_target(
        self, citation: sqlite3.Row, source: sqlite3.Row, target: sqlite3.Row
    ) -> tuple[int, str, bool]:
        target_doc = self.conn.execute("SELECT * FROM documents WHERE id = ?", (target["document_id"],)).fetchone()
        same_version = target["document_id"] == source["id"]
        exact_level = (
            citation["ref_type"] in {"previous_paragraph", "current_chapter"}
            or (citation["requested_paragraph"] and target["level"] == "paragraph")
            or (citation["requested_item"] and target["level"] == "item")
            or (
                not citation["requested_paragraph"]
                and not citation["requested_item"]
                and target["level"] == "article"
            )
        )
        score = 0
        reasons: list[str] = []
        if same_version:
            score += 100
            reasons.append("same_version")
        if exact_level:
            score += 50
            reasons.append("exact_requested_level")
        else:
            score -= 20
            reasons.append("article_level_fallback")
        if target_doc["status"] == "active":
            score += 10
            reasons.append("target_active")
        elif target_doc["status"] == "superseded":
            score += 5
            reasons.append("target_superseded")
        if source["effective_date"] and target_doc["effective_date"]:
            if target_doc["effective_date"] <= source["effective_date"]:
                score += 30
                reasons.append("effective_at_or_before_source")
                score += max(0, 30 - abs(len(target_doc["version_label"]) - len(source["version_label"])))
            else:
                score -= 40
                reasons.append("target_effective_after_source")
        return score, "+".join(reasons), bool(same_version and exact_level)

    def resolve_citation(self, citation_id: str) -> dict[str, Any]:
        citation = self._row("SELECT * FROM citations WHERE id = ?", (citation_id,))
        source = self._row("SELECT * FROM documents WHERE id = ?", (citation["document_id"],))
        with self.write_lock, self.transaction() as conn:
            conn.execute("DELETE FROM candidates WHERE citation_id = ?", (citation_id,))
            targets = self._possible_targets(citation, source)
            scored: list[tuple[sqlite3.Row, int, str, bool]] = []
            for target in targets:
                score, reason, exact = self._score_target(citation, source, target)
                scored.append((target, score, reason, exact))
            scored.sort(key=lambda item: (-item[1], item[0]["locator"], item[0]["id"]))
            for ordinal, (target, score, reason, exact) in enumerate(scored, start=1):
                conn.execute(
                    "INSERT INTO candidates(id, citation_id, target_clause_id, ordinal, score, reason, exact) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (self._id("cand", citation_id, target["id"]), citation_id, target["id"], ordinal, score, reason, int(exact)),
                )
            decisions = self._active_decisions_for(citation, source["version_label"], conn)
            mapped_targets = {
                decision["id"]: self._decision_target_for_version(decision, citation, conn)
                for decision in decisions
            }
            unique_targets = {target for target in mapped_targets.values() if target}
            selected_id = None
            status = "resolved" if scored else "unresolved"
            reason = "highest_scoring_structural_candidate" if scored else "no_candidate_found"
            if len(unique_targets) > 1:
                status = "divergent"
                reason = "researcher_decisions_select_different_targets"
            elif len(decisions) == 1 and mapped_targets.get(decisions[0]["id"]):
                selected_id = mapped_targets.get(decisions[0]["id"])
                status = "resolved_manual"
                reason = "single_researcher_decision"
            elif unique_targets:
                selected_id = next(iter(unique_targets))
                status = "resolved_manual_consensus"
                reason = "multiple_researchers_select_same_target"
            elif scored:
                selected_id = scored[0][0]["id"]
            if selected_id and not any(target["id"] == selected_id for target, *_ in scored):
                selected_clause = conn.execute("SELECT * FROM clauses WHERE id = ?", (selected_id,)).fetchone()
                if selected_clause:
                    candidate_id = self._id("cand", citation_id, selected_id)
                    if not conn.execute("SELECT id FROM candidates WHERE id = ?", (candidate_id,)).fetchone():
                        conn.execute(
                            "INSERT INTO candidates(id, citation_id, target_clause_id, ordinal, score, reason, exact) VALUES (?, ?, ?, 0, 0, ?, 0)",
                            (candidate_id, citation_id, selected_id, "manual_decision_mapped_to_version"),
                        )
            candidate_pk = self._id("cand", citation_id, selected_id) if selected_id else None
            conn.execute(
                "UPDATE citations SET selected_candidate_id = ?, resolution_status = ?, resolution_reason = ?, updated_at = ? WHERE id = ?",
                (candidate_pk, status, reason, utc_now(), citation_id),
            )
        return self.get_citation(citation_id)

    def _decision_target_for_version(
        self, decision: sqlite3.Row, citation: sqlite3.Row, conn: sqlite3.Connection
    ) -> str | None:
        row = conn.execute(
            "SELECT id FROM clauses WHERE document_id = ? AND semantic_key = ? ORDER BY ordinal, id LIMIT 1",
            (citation["document_id"], decision["target_semantic_key"]),
        ).fetchone()
        if row:
            return row["id"]
        stored = conn.execute(
            "SELECT document_id FROM clauses WHERE id = ?", (decision["target_clause_id"],)
        ).fetchone()
        return decision["target_clause_id"] if stored else None

    def _active_decisions_for(
        self, citation: sqlite3.Row, version_label: str, conn: sqlite3.Connection | None = None
    ) -> list[sqlite3.Row]:
        conn = conn or self.conn
        rows = conn.execute(
            """SELECT * FROM decisions WHERE citation_semantic_key = ? AND status = 'active'
               AND (applies_from_version IS NULL OR applies_from_version <= ?)
               AND (applies_to_version IS NULL OR ? <= applies_to_version)
               ORDER BY created_at""",
            (citation["semantic_key"], version_label, version_label),
        ).fetchall()
        return list(rows)

    def resolve_all_citations(self) -> None:
        rows = self.conn.execute("SELECT id FROM citations ORDER BY id").fetchall()
        for row in rows:
            self.resolve_citation(row["id"])

    def transition_document(self, document_id: str, to_status: str, *, expected_version: int, reason: str = "") -> dict[str, Any]:
        allowed = {
            "ready": {"active"},
            "active": {"superseded"},
            "superseded": {"repealed"},
            "repealed": set(),
        }
        document = self._document(document_id)
        if document["lock_version"] != expected_version:
            current = self._document(document_id)
            raise ConflictError(
                "document lock_version mismatch",
                {
                    "expected_by_client": expected_version,
                    "current": current,
                    "client_request": {"status": to_status, "reason": reason},
                },
            )
        if to_status not in allowed.get(document["status"], set()):
            raise IllegalTransitionError(
                f"illegal transition {document['status']} -> {to_status}"
            )
        now = utc_now()
        with self.write_lock, self.transaction() as conn:
            conn.execute(
                "UPDATE documents SET status = ?, lock_version = lock_version + 1, updated_at = ? WHERE id = ?",
                (to_status, now, document_id),
            )
            conn.execute(
                "INSERT INTO document_transitions(document_id, from_status, to_status, changed_at, reason) VALUES (?, ?, ?, ?, ?)",
                (document_id, document["status"], to_status, now, reason),
            )
        self.resolve_all_citations()
        return self._document(document_id)

    def document_timeline(self, document_id: str) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in self.conn.execute(
                "SELECT * FROM document_transitions WHERE document_id = ? ORDER BY id", (document_id,)
            ).fetchall()
        ]

    def correct_citation(
        self,
        citation_id: str,
        *,
        researcher: str,
        target_clause_id: str,
        rationale: str = "",
        applies_from_version: str | None = None,
        applies_to_version: str | None = None,
        expected_version: int,
    ) -> dict[str, Any]:
        citation = self.get_citation(citation_id)
        target = self._clause(target_clause_id)
        if citation["lock_version"] != expected_version:
            raise ConflictError(
                "citation lock_version mismatch",
                {
                    "expected_by_client": expected_version,
                    "current": citation,
                    "client_request": {
                        "researcher": researcher,
                        "target_clause_id": target_clause_id,
                        "rationale": rationale,
                    },
                },
            )
        now = utc_now()
        previous = [
            decision
            for decision in citation["decisions"]
            if decision["status"] == "active" and decision["researcher"] != researcher
            and decision["target_clause_id"] != target_clause_id
        ]
        if previous:
            raise ConflictError(
                "another researcher selected a different target",
                {
                    "current_citation": citation,
                    "existing_context": previous,
                    "client_request": {
                        "researcher": researcher,
                        "target_clause_id": target_clause_id,
                        "rationale": rationale,
                        "applies_from_version": applies_from_version,
                        "applies_to_version": applies_to_version,
                    },
                    "merge_hint": "POST /api/citations/{id}/merge with both target IDs to preserve both contexts",
                },
            )
        decision_id = self._id(
            "dec",
            citation["semantic_key"],
            researcher,
        )
        with self.write_lock, self.transaction() as conn:
            conn.execute(
                "UPDATE decisions SET status = 'superseded', revoked_at = ? WHERE citation_semantic_key = ? AND researcher = ? AND status = 'active'",
                (now, citation["semantic_key"], researcher),
            )
            conn.execute(
                """INSERT INTO decisions(id, citation_semantic_key, source_citation_id, source_version_label,
                   source_locator, original_sentence, quote, researcher, target_clause_id, target_semantic_key,
                   rationale, applies_from_version, applies_to_version, status, created_at, revoked_at,
                   merge_parent_id, lock_version)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, NULL, NULL, 1)
                   ON CONFLICT(id) DO UPDATE SET rationale = excluded.rationale,
                     applies_from_version = excluded.applies_from_version,
                     applies_to_version = excluded.applies_to_version,
                     target_clause_id = excluded.target_clause_id,
                     target_semantic_key = excluded.target_semantic_key,
                     status = 'active',
                     revoked_at = NULL,
                     created_at = excluded.created_at,
                     lock_version = lock_version + 1""",
                (
                    decision_id,
                    citation["semantic_key"],
                    citation_id,
                    self._document(citation["document_id"])["version_label"],
                    citation["anchor_locator"],
                    citation["sentence"],
                    citation["quote"],
                    researcher,
                    target_clause_id,
                    target["semantic_key"],
                    rationale,
                    applies_from_version,
                    applies_to_version,
                    now,
                ),
            )
            conn.execute(
                "UPDATE citations SET lock_version = lock_version + 1, updated_at = ? WHERE id = ?",
                (now, citation_id),
            )
        self.resolve_affected(citation["semantic_key"])
        return self.get_citation(citation_id)

    def merge_citation_corrections(
        self,
        citation_id: str,
        *,
        researcher: str,
        first: dict[str, Any],
        second: dict[str, Any],
        rationale: str,
    ) -> dict[str, Any]:
        citation = self.get_citation(citation_id)
        target_ids = {first["target_clause_id"], second["target_clause_id"]}
        if len(target_ids) != 2:
            raise ValueError("merge requires two different target_clause_id values")
        now = utc_now()
        records = [
            (
                first,
                self._clause(first["target_clause_id"]),
                first.get("researcher", researcher),
            ),
            (
                second,
                self._clause(second["target_clause_id"]),
                second.get("researcher", researcher),
            ),
        ]
        source_version = self._document(citation["document_id"])["version_label"]
        with self.write_lock, self.transaction() as conn:
            previous_id = None
            for payload, target, payload_researcher in records:
                decision_id = self._id(
                    "dec",
                    citation["semantic_key"],
                    payload_researcher,
                    target["id"],
                    payload.get("rationale", ""),
                    payload.get("applies_from_version"),
                    payload.get("applies_to_version"),
                )
                conn.execute(
                    """INSERT INTO decisions(id, citation_semantic_key, source_citation_id, source_version_label,
                       source_locator, original_sentence, quote, researcher, target_clause_id, target_semantic_key,
                       rationale, applies_from_version, applies_to_version, status, created_at, revoked_at,
                       merge_parent_id, lock_version)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, NULL, ?, 1)
                       ON CONFLICT(id) DO UPDATE SET rationale = excluded.rationale,
                         applies_from_version = excluded.applies_from_version,
                         applies_to_version = excluded.applies_to_version,
                         merge_parent_id = excluded.merge_parent_id,
                         lock_version = lock_version + 1""",
                    (
                        decision_id,
                        citation["semantic_key"],
                        citation_id,
                        source_version,
                        citation["anchor_locator"],
                        citation["sentence"],
                        citation["quote"],
                        payload_researcher,
                        target["id"],
                        target["semantic_key"],
                        payload.get("rationale", ""),
                        payload.get("applies_from_version"),
                        payload.get("applies_to_version"),
                        now,
                        previous_id,
                    ),
                )
                previous_id = decision_id
            conn.execute(
                "UPDATE citations SET lock_version = lock_version + 1, updated_at = ? WHERE id = ?",
                (now, citation_id),
            )
        self.resolve_affected(citation["semantic_key"])
        return self.get_citation(citation_id)

    def reparse_affected(self, citation_semantic_key: str) -> dict[str, Any]:
        self.resolve_affected(citation_semantic_key)
        return {"reparse_trigger": citation_semantic_key}

    def resolve_affected(self, semantic_key: str) -> None:
        citation_ids = [
            row["id"]
            for row in self.conn.execute(
                "SELECT id FROM citations WHERE semantic_key = ? ORDER BY id", (semantic_key,)
            ).fetchall()
        ]
        downstream = []
        for citation_id in citation_ids:
            citation = self.get_citation(citation_id)
            source_key = citation["source_semantic_key"]
            related = self.conn.execute(
                """SELECT cit.id FROM citations cit JOIN clauses cl ON cit.source_clause_id = cl.id
                   WHERE cl.semantic_key = ? AND cit.id != ?""",
                (source_key, citation_id),
            ).fetchall()
            downstream.extend(row["id"] for row in related)
        for citation_id in citation_ids + downstream:
            self.resolve_citation(citation_id)

    def _candidate_clause(self, candidate_id: str | None) -> sqlite3.Row | None:
        if not candidate_id:
            return None
        return self.conn.execute(
            """SELECT cl.*, d.doc_key AS document_doc_key, d.version_label, d.status AS document_status,
                      d.effective_date AS document_effective_date, d.repealed_date AS document_repealed_date
               FROM candidates c JOIN clauses cl ON c.target_clause_id = cl.id
               JOIN documents d ON cl.document_id = d.id WHERE c.id = ?""",
            (candidate_id,),
        ).fetchone()

    def _citation_edges(self) -> list[dict[str, Any]]:
        edges: list[dict[str, Any]] = []
        for citation in self.list_citations():
            source_doc = self._document(citation["document_id"])
            source_clause = self._clause(citation["source_clause_id"])
            selected = None
            for candidate in citation["candidates"]:
                if candidate["id"] == citation["selected_candidate_id"]:
                    selected = candidate
                    break
            alternatives = self._alternative_targets(citation)
            edges.append(
                {
                    "key": citation["semantic_key"],
                    "citation_id": citation["id"],
                    "doc_key": source_doc["doc_key"],
                    "source_document_id": source_doc["id"],
                    "source_version_label": source_doc["version_label"],
                    "source_document_status": source_doc["status"],
                    "source_effective_date": source_doc["effective_date"],
                    "source_repealed_date": source_doc["repealed_date"],
                    "source_clause_id": source_clause["id"],
                    "source_semantic_key": source_clause["semantic_key"],
                    "source_locator": source_clause["locator"],
                    "sentence": citation["sentence"],
                    "quote": citation["quote"],
                    "target_clause_id": selected["target_clause_id"] if selected else None,
                    "target_semantic_key": selected["target_semantic_key"] if selected else None,
                    "target_locator": selected["target_locator"] if selected else None,
                    "target_version_label": selected["target_version_label"] if selected else None,
                    "target_document_status": selected["target_document_status"] if selected else None,
                    "target_effective_date": selected["target_effective_date"] if selected else None,
                    "target_repealed_date": selected["target_repealed_date"] if selected else None,
                    "resolution_status": citation["resolution_status"],
                    "resolution_reason": citation["resolution_reason"],
                    "alternative_targets": alternatives,
                    "manual": citation["resolution_status"].startswith("resolved_manual")
                    or citation["resolution_status"] == "divergent",
                }
            )
        return edges

    def _edge_state_at(self, edge: dict[str, Any], at_date: str) -> str:
        if edge["source_document_status"] == "ready" or not edge["source_effective_date"] or edge["source_effective_date"] > at_date:
            return "source_not_effective"
        if edge["source_repealed_date"] and edge["source_repealed_date"] <= at_date:
            return "source_repealed"
        if edge["source_document_status"] == "superseded":
            successor = self._successor_effective_date(edge["doc_key"], edge["source_effective_date"])
            if successor and successor <= at_date:
                return "source_superseded"
        if not edge["target_clause_id"]:
            return "target_missing"
        if edge["target_repealed_date"] and edge["target_repealed_date"] <= at_date:
            return "target_repealed"
        if edge["target_effective_date"] and edge["target_effective_date"] > at_date:
            return "target_not_effective"
        if edge["target_document_status"] == "superseded":
            target_doc_key = edge["target_semantic_key"].split("|", 1)[0]
            successor = self._successor_effective_date(target_doc_key, edge["target_effective_date"])
            if successor and successor <= at_date:
                return "target_superseded"
        return "active"

    def _successor_effective_date(self, doc_key: str, current_effective: str | None) -> str | None:
        if not current_effective:
            return None
        row = self.conn.execute(
            """SELECT MIN(effective_date) AS next_date FROM documents
               WHERE doc_key = ? AND effective_date IS NOT NULL AND effective_date > ?""",
            (doc_key, current_effective),
        ).fetchone()
        return row["next_date"] if row else None

    def graph_at(self, at_date: str) -> dict[str, Any]:
        edges = []
        nodes: dict[str, dict[str, Any]] = {}
        for edge in self._citation_edges():
            state = self._edge_state_at(edge, at_date)
            enriched = {**edge, "state": state, "at_date": at_date}
            edges.append(enriched)
            source_clause = self._clause(edge["source_clause_id"])
            nodes[source_clause["id"]] = {
                "id": source_clause["id"],
                "semantic_key": source_clause["semantic_key"],
                "locator": source_clause["locator"],
                "level": source_clause["level"],
                "version_label": edge["source_version_label"],
            }
            if edge["target_clause_id"]:
                target_clause = self._clause(edge["target_clause_id"])
                target_doc = self._document(target_clause["document_id"])
                nodes[target_clause["id"]] = {
                    "id": target_clause["id"],
                    "semantic_key": target_clause["semantic_key"],
                    "locator": target_clause["locator"],
                    "level": target_clause["level"],
                    "version_label": target_doc["version_label"],
                }
        counts: dict[str, int] = {}
        for edge in edges:
            counts[edge["state"]] = counts.get(edge["state"], 0) + 1
        return {
            "at_date": at_date,
            "nodes": sorted(nodes.values(), key=lambda node: node["id"]),
            "edges": sorted(edges, key=lambda edge: edge["key"]),
            "counts": dict(sorted(counts.items())),
        }

    def compare_graphs(self, left_date: str, right_date: str) -> dict[str, Any]:
        left = {edge["key"]: edge for edge in self.graph_at(left_date)["edges"]}
        right = {edge["key"]: edge for edge in self.graph_at(right_date)["edges"]}
        added, disappeared, redirected, changed_state, unchanged = [], [], [], [], []
        for edge_key in sorted(set(left) | set(right)):
            before = left.get(edge_key)
            after = right.get(edge_key)
            if before and not after:
                disappeared.append(before)
            elif after and not before:
                added.append(after)
            elif before and after:
                if before["state"] != "active" and after["state"] == "active":
                    added.append(after)
                elif before["state"] == "active" and after["state"] != "active":
                    disappeared.append(before)
                elif (
                    before["target_clause_id"] != after["target_clause_id"]
                    or before["target_semantic_key"] != after["target_semantic_key"]
                ):
                    redirected.append({"before": before, "after": after})
                elif before["state"] != after["state"]:
                    changed_state.append({"before": before, "after": after})
                else:
                    unchanged.append({"before": before, "after": after})
        return {
            "left_date": left_date,
            "right_date": right_date,
            "added": sorted(added, key=lambda edge: edge["key"]),
            "disappeared": sorted(disappeared, key=lambda edge: edge["key"]),
            "redirected": sorted(redirected, key=lambda pair: pair["after"]["key"]),
            "changed_state": sorted(changed_state, key=lambda pair: pair["after"]["key"]),
            "unchanged_count": len(unchanged),
        }

    def shortest_chain(self, source_clause_id: str, target_clause_id: str, at_date: str) -> dict[str, Any]:
        graph = self.graph_at(at_date)
        active_edges = [edge for edge in graph["edges"] if edge["state"] in {"active", "target_superseded", "source_superseded"}]
        adjacency: dict[str, list[dict[str, Any]]] = {}
        for edge in active_edges:
            if edge["target_clause_id"]:
                adjacency.setdefault(edge["source_clause_id"], []).append(edge)
        queue = [(source_clause_id, [])]
        seen = {source_clause_id}
        while queue:
            current, path = queue.pop(0)
            if current == target_clause_id:
                return {"found": True, "length": len(path), "chain": path, "at_date": at_date}
            for edge in adjacency.get(current, []):
                nxt = edge["target_clause_id"]
                if nxt not in seen:
                    seen.add(nxt)
                    queue.append((nxt, path + [edge]))
        return {"found": False, "length": None, "chain": [], "at_date": at_date}

    def timeline(self, at_date: str | None = None) -> dict[str, Any]:
        documents = [
            {
                "type": "document",
                "date": document["effective_date"] or document["created_at"][:10],
                "document": self._document(document["id"]),
            }
            for document in self.list_documents()
        ]
        decisions = [
            {
                "type": "decision",
                "date": row["created_at"][:10],
                "decision": dict(row),
            }
            for row in self.conn.execute("SELECT * FROM decisions ORDER BY created_at").fetchall()
        ]
        transitions = [
            {
                "type": "document_transition",
                "date": row["changed_at"][:10],
                "transition": {
                    "document_id": row["document_id"],
                    "from_status": row["from_status"],
                    "to_status": row["to_status"],
                    "reason": row["reason"],
                },
            }
            for row in self.conn.execute(
                """SELECT dt.* FROM document_transitions dt JOIN documents d ON dt.document_id = d.id
                   ORDER BY dt.changed_at, dt.id"""
            ).fetchall()
        ]
        events = sorted(documents + transitions + decisions, key=lambda event: (event["date"], event["type"]))
        return {"at_date": at_date, "events": events}

    def export_package(self) -> dict[str, Any]:
        documents = []
        clauses = []
        citations = []
        candidates = []
        decisions = []
        for document in self.list_documents():
            documents.append(
                {
                    "id": document["id"],
                    "doc_key": document["doc_key"],
                    "version_label": document["version_label"],
                    "title": document["title"],
                    "raw_text": document["raw_text"],
                    "normalized_text": document["normalized_text"],
                    "source_fingerprint": document["source_fingerprint"],
                    "status": document["status"],
                    "effective_date": document["effective_date"],
                    "repealed_date": document["repealed_date"],
                    "lock_version": document["lock_version"],
                    "parser_version": document["parser_version"],
                }
            )
        for row in self.conn.execute("SELECT * FROM clauses ORDER BY id").fetchall():
            clauses.append(
                {
                    "id": row["id"],
                    "document_id": row["document_id"],
                    "semantic_key": row["semantic_key"],
                    "locator": row["locator"],
                    "level": row["level"],
                    "number": row["number"],
                    "number_value": row["number_value"],
                    "ordinal": row["ordinal"],
                    "text": row["text"],
                    "normalized_text": row["normalized_text"],
                    "text_fingerprint": row["text_fingerprint"],
                    "normalized_fingerprint": row["normalized_fingerprint"],
                    "parent_semantic_key": row["parent_semantic_key"],
                    "chapter_semantic_key": row["chapter_semantic_key"],
                    "article_semantic_key": row["article_semantic_key"],
                    "previous_paragraph_semantic_key": row["previous_paragraph_semantic_key"],
                }
            )
        for citation in self.list_citations():
            citations.append(
                {
                    "id": citation["id"],
                    "document_id": citation["document_id"],
                    "semantic_key": citation["semantic_key"],
                    "source_clause_id": citation["source_clause_id"],
                    "source_semantic_key": citation["source_semantic_key"],
                    "sentence": citation["sentence"],
                    "quote": citation["quote"],
                    "ref_type": citation["ref_type"],
                    "requested_article": citation["requested_article"],
                    "requested_paragraph": citation["requested_paragraph"],
                    "requested_item": citation["requested_item"],
                    "anchor": {
                        "locator": citation["anchor_locator"],
                        "line_number": citation["line_number"],
                        "char_start": citation["char_start"],
                        "char_end": citation["char_end"],
                        "clause_fingerprint": citation["anchor_fingerprint"],
                        "normalized_clause_fingerprint": citation["normalized_anchor_fingerprint"],
                    },
                    "selected_candidate_id": citation["selected_candidate_id"],
                    "resolution_status": citation["resolution_status"],
                    "resolution_reason": citation["resolution_reason"],
                    "lock_version": citation["lock_version"],
                }
            )
            for candidate in citation["candidates"]:
                candidates.append(
                    {
                        "id": candidate["id"],
                        "citation_id": candidate["citation_id"],
                        "target_clause_id": candidate["target_clause_id"],
                        "target_semantic_key": candidate["target_semantic_key"],
                        "target_locator": candidate["target_locator"],
                        "target_version_label": candidate["target_version_label"],
                        "ordinal": candidate["ordinal"],
                        "score": candidate["score"],
                        "reason": candidate["reason"],
                        "exact": bool(candidate["exact"]),
                    }
                )
            for decision in citation["decisions"]:
                decisions.append(
                    {
                        "id": decision["id"],
                        "citation_semantic_key": decision["citation_semantic_key"],
                        "source_citation_id": decision["source_citation_id"],
                        "source_version_label": decision["source_version_label"],
                        "source_locator": decision["source_locator"],
                        "original_sentence": decision["original_sentence"],
                        "quote": decision["quote"],
                        "researcher": decision["researcher"],
                        "target_clause_id": decision["target_clause_id"],
                        "target_semantic_key": decision["target_semantic_key"],
                        "rationale": decision["rationale"],
                        "applies_from_version": decision["applies_from_version"],
                        "applies_to_version": decision["applies_to_version"],
                        "status": decision["status"],
                        "merge_parent_id": decision["merge_parent_id"],
                        "lock_version": decision["lock_version"],
                    }
                )
        package = {
            "format": "citeweave.citation-package",
            "format_version": 1,
            "parser_version": PARSER_VERSION,
            "documents": documents,
            "clauses": sorted(clauses, key=lambda item: item["id"]),
            "citations": sorted(citations, key=lambda item: item["id"]),
            "candidates": sorted(candidates, key=lambda item: item["id"]),
            "decisions": sorted(decisions, key=lambda item: item["id"]),
        }
        package["package_fingerprint"] = hashlib.sha256(canonical_json(package).encode("utf-8")).hexdigest()
        return package

    def publish_snapshot(self, label: str, at_date: str, created_by: str) -> dict[str, Any]:
        package = self.export_package()
        snapshot_id = self._id("snap", label)
        payload = canonical_json({"label": label, "at_date": at_date, "created_by": created_by, "package": package})
        with self.write_lock, self.transaction() as conn:
            conn.execute(
                """INSERT INTO snapshots(id, label, at_date, created_by, created_at, package_fingerprint, payload)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (snapshot_id, label, at_date, created_by, utc_now(), package["package_fingerprint"], payload),
            )
        return self.get_snapshot(label)

    def get_snapshot(self, label: str) -> dict[str, Any]:
        row = self._row("SELECT * FROM snapshots WHERE label = ?", (label,))
        return {
            "id": row["id"],
            "label": row["label"],
            "at_date": row["at_date"],
            "created_by": row["created_by"],
            "created_at": row["created_at"],
            "package_fingerprint": row["package_fingerprint"],
            "payload": json.loads(row["payload"]),
        }

    def list_snapshots(self) -> list[dict[str, Any]]:
        return [
            {
                "id": row["id"],
                "label": row["label"],
                "at_date": row["at_date"],
                "created_by": row["created_by"],
                "created_at": row["created_at"],
                "package_fingerprint": row["package_fingerprint"],
            }
            for row in self.conn.execute("SELECT * FROM snapshots ORDER BY label").fetchall()
        ]

    def verify_anchor_text(self, document_id: str, candidate_text: str) -> dict[str, Any]:
        document = self._document(document_id)
        candidate_normalized = normalize_text(candidate_text)
        raw_same = fingerprint(candidate_text.replace("\r\n", "\n").replace("\r", "\n")) == document["source_fingerprint"]
        normalized_same = candidate_normalized == document["normalized_text"]
        if raw_same:
            status = "valid"
        elif normalized_same:
            status = "wrapping_changed"
        else:
            status = "invalid"
        return {
            "document_id": document_id,
            "parser_version": document["parser_version"],
            "stored_fingerprint": document["source_fingerprint"],
            "candidate_fingerprint": fingerprint(candidate_text),
            "normalized_fingerprint": fingerprint(candidate_normalized),
            "status": status,
            "anchors_still_apply_by_locator": status != "invalid",
        }
