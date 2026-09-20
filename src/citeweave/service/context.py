"""In-memory graph context loaded from SQLite (used by the resolver)."""
from __future__ import annotations

from ..resolver import VCitation, VUnit, VVersion


def load_context(db) -> tuple[list[VVersion], dict[int, str]]:
    versions: list[VVersion] = []
    rows = db.query(
        "SELECT id, document_id, version_label, state, effective_date, "
        "end_date, title FROM versions WHERE import_stage='final'")
    for row in rows:
        versions.append(VVersion(
            version_id=row["id"], document_id=row["document_id"],
            label=row["version_label"], state=row["state"],
            effective_date=row["effective_date"], end_date=row["end_date"],
            title=row["title"]))
    by_version = {v.version_id: v for v in versions}
    units = db.query(
        "SELECT id, version_id, key, kind, num, title, start_offset, "
        "end_offset, level, ordinal, article_num, path FROM units "
        "WHERE version_id IN (SELECT id FROM versions WHERE import_stage='final') "
        "ORDER BY version_id, ordinal")
    import json as _json
    for row in units:
        by_version[row["version_id"]].units.append(VUnit(
            unit_id=row["id"], version_id=row["version_id"], key=row["key"],
            kind=row["kind"], num=row["num"], title=row["title"],
            start=row["start_offset"], end=row["end_offset"],
            level=row["level"], ordinal=row["ordinal"],
            article_num=row["article_num"],
            path=tuple(_json.loads(row["path"]))))
    doc_rows = db.query("SELECT id, code FROM documents")
    docs = {row["id"]: row["code"] for row in doc_rows}
    return versions, docs


def load_citations(db) -> list[VCitation]:
    import json as _json
    rows = db.query(
        "SELECT id, version_id, source_article_num, paragraph_idx, kind, "
        "descriptor FROM citations ORDER BY id")
    return [VCitation(
        citation_id=row["id"], version_id=row["version_id"],
        source_article_num=row["source_article_num"],
        paragraph_idx=row["paragraph_idx"], kind=row["kind"],
        descriptor=_json.loads(row["descriptor"])) for row in rows]
