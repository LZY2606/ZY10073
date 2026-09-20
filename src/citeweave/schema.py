"""SQLite DDL for CiteWeave."""
from __future__ import annotations

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS documents (
    id INTEGER PRIMARY KEY,
    code TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    current_version_id INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS versions (
    id INTEGER PRIMARY KEY,
    document_id INTEGER NOT NULL REFERENCES documents(id),
    version_label TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'adopted',
    adopted_date TEXT,
    effective_date TEXT NOT NULL,
    end_date TEXT,
    title TEXT NOT NULL,
    raw_text TEXT NOT NULL,
    raw_fingerprint TEXT NOT NULL,
    norm_fingerprint TEXT NOT NULL,
    parser_version TEXT NOT NULL,
    import_stage TEXT NOT NULL DEFAULT 'final',  -- staged | final | abandoned
    staged_at TEXT,
    idempotency_key TEXT,
    article_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    UNIQUE (document_id, version_label)
);
CREATE INDEX IF NOT EXISTS idx_versions_doc ON versions(document_id);
CREATE INDEX IF NOT EXISTS idx_versions_stage ON versions(import_stage);

CREATE TABLE IF NOT EXISTS units (
    id INTEGER PRIMARY KEY,
    version_id INTEGER NOT NULL REFERENCES versions(id),
    key TEXT NOT NULL,
    kind TEXT NOT NULL,
    num TEXT,
    title TEXT NOT NULL,
    start_offset INTEGER NOT NULL,
    end_offset INTEGER NOT NULL,
    level INTEGER NOT NULL DEFAULT 0,
    ordinal INTEGER NOT NULL,
    article_num TEXT,
    path TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_units_version ON units(version_id);
CREATE INDEX IF NOT EXISTS idx_units_article ON units(version_id, kind, num);

CREATE TABLE IF NOT EXISTS citations (
    id INTEGER PRIMARY KEY,
    version_id INTEGER NOT NULL REFERENCES versions(id),
    source_article_num TEXT,
    paragraph_idx INTEGER,
    kind TEXT NOT NULL,
    text TEXT NOT NULL,
    start_offset INTEGER NOT NULL,
    end_offset INTEGER NOT NULL,
    sentence TEXT NOT NULL,
    sent_start INTEGER NOT NULL,
    signature TEXT NOT NULL,
    descriptor TEXT NOT NULL,
    decision_rev INTEGER NOT NULL DEFAULT 0,
    UNIQUE (version_id, start_offset, end_offset)
);
CREATE INDEX IF NOT EXISTS idx_citations_version ON citations(version_id);

CREATE TABLE IF NOT EXISTS candidates (
    id INTEGER PRIMARY KEY,
    citation_id INTEGER NOT NULL REFERENCES citations(id),
    rank INTEGER NOT NULL,
    score REAL NOT NULL,
    target_version_id INTEGER REFERENCES versions(id),
    target_unit_id INTEGER REFERENCES units(id),
    target_desc TEXT NOT NULL,             -- human readable locator
    status TEXT NOT NULL,                  -- valid|repealed|missing|ambiguous
    basis TEXT NOT NULL,                   -- JSON, why this candidate
    UNIQUE (citation_id, rank)
);
CREATE INDEX IF NOT EXISTS idx_candidates_cite ON candidates(citation_id);

CREATE TABLE IF NOT EXISTS resolutions (
    id INTEGER PRIMARY KEY,
    citation_id INTEGER NOT NULL UNIQUE REFERENCES citations(id),
    selected_candidate_id INTEGER REFERENCES candidates(id),
    status TEXT NOT NULL,                  -- resolved|ambiguous|missing|repealed|cross_version_conflict
    basis TEXT NOT NULL,                  -- JSON, machine-readable rationale
    parser_version TEXT NOT NULL,
    resolved_at TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY,
    citation_id INTEGER NOT NULL REFERENCES citations(id),
    researcher TEXT NOT NULL,
    action TEXT NOT NULL,                 -- override|version_range|clear
    target_version_id INTEGER REFERENCES versions(id),
    target_unit_id INTEGER REFERENCES units(id),
    valid_from TEXT,
    valid_to TEXT,
    note TEXT NOT NULL DEFAULT '',
    source_page TEXT NOT NULL DEFAULT '',
    base_rev INTEGER NOT NULL,            -- citation decision_rev seen
    created_at TEXT NOT NULL,
    UNIQUE (citation_id, researcher)
);
CREATE INDEX IF NOT EXISTS idx_decisions_cite ON decisions(citation_id);

CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY,
    label TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'open',   -- open -> published
    payload_fingerprint TEXT NOT NULL,
    body TEXT NOT NULL,                    -- immutable bundle
    parser_version TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    published_at TEXT
);

CREATE TABLE IF NOT EXISTS merge_sessions (
    id INTEGER PRIMARY KEY,
    label TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'open',   -- open -> merged
    base_version_id INTEGER NOT NULL REFERENCES versions(id),
    researcher_a TEXT NOT NULL,
    researcher_b TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    merged_at TEXT
);

CREATE TABLE IF NOT EXISTS merge_items (
    id INTEGER PRIMARY KEY,
    session_id INTEGER NOT NULL REFERENCES merge_sessions(id),
    citation_id INTEGER NOT NULL REFERENCES citations(id),
    side TEXT NOT NULL,                   -- A | B
    researcher TEXT NOT NULL,
    target_version_id INTEGER,
    target_unit_id INTEGER,
    valid_from TEXT,
    valid_to TEXT,
    note TEXT NOT NULL DEFAULT '',
    source_page TEXT NOT NULL DEFAULT '',
    quote TEXT NOT NULL DEFAULT '',
    context TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_merge_items_session ON merge_items(session_id);

CREATE TABLE IF NOT EXISTS idempotent_requests (
    request_key TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    result_status INTEGER NOT NULL,
    result_body TEXT NOT NULL
);
"""
