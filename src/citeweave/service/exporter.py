"""Deterministic citation bundles: fingerprints, anchors, parser version."""
from __future__ import annotations

import io
import json
import zipfile
from typing import Any

from .. import PARSER_VERSION
from ..textutils import anchor_status, content_fingerprint, raw_fingerprint

# Fixed ZIP timestamp (1980-01-01) keeps bytes reproducible.
_ZIP_DATE = (1980, 1, 1, 0, 0, 0)


def _dump(data: Any) -> bytes:
    return (json.dumps(data, ensure_ascii=False, sort_keys=True,
                       indent=2, separators=(",", ": ")) + "\n").encode("utf-8")


def _bundle_data(self) -> dict[str, Any]:
    versions = self.db.query(
        "SELECT v.id,d.code,v.version_label,v.state,v.effective_date,"
        "v.end_date,v.title,v.raw_text,v.raw_fingerprint,v.norm_fingerprint "
        "FROM versions v JOIN documents d ON d.id=v.document_id "
        "WHERE v.import_stage='final' ORDER BY v.id")
    citations = self.db.query(
        "SELECT id,version_id,source_article_num,paragraph_idx,kind,text,"
        "start_offset,end_offset,sentence,sent_start,signature,descriptor "
        "FROM citations ORDER BY id")
    candidates = self.db.query(
        "SELECT citation_id,rank,target_version_id,target_unit_id,"
        "target_desc,status,score,basis FROM candidates ORDER BY citation_id,rank")
    resolutions = self.db.query(
        "SELECT citation_id,status,basis,parser_version,revision "
        "FROM resolutions ORDER BY citation_id")
    anchors = []
    for row in citations:
        anchors.append({
            "citation_id": row["id"],
            "version_id": row["version_id"],
            "start": row["start_offset"],
            "end": row["end_offset"],
            "quote": row["text"],
            "signature": row["signature"],
            "raw_fingerprint": None,
        })
    version_hashes = {row["id"]: (row["raw_fingerprint"],
                                  row["norm_fingerprint"]) for row in versions}
    for anchor in anchors:
        anchor["raw_fingerprint"] = version_hashes[anchor["version_id"]][0]
        anchor["norm_fingerprint"] = version_hashes[anchor["version_id"]][1]
    return {
        "manifest": {
            "tool": "citeweave",
            "parser_version": PARSER_VERSION,
            "version_count": len(versions),
            "citation_count": len(citations),
        },
        "versions": [dict(r) for r in versions],
        "citations": [dict(r) for r in citations],
        "candidates": [dict(r) for r in candidates],
        "resolutions": [dict(r) for r in resolutions],
        "anchors": anchors,
    }


DATA_NAMES = ("versions", "citations", "candidates", "resolutions", "anchors")


def determinism_manifest(data: dict[str, Any]) -> dict[str, str]:
    """Per-file sha256 plus an overall content digest.

    ``manifest.json`` carries the hashes of the five immutable data files;
    it does not hash itself (that would be self-referential).
    """
    files = {f"citeweave/{name}.json": _dump(data[name])
             for name in DATA_NAMES}
    overall_src = b"".join(h for name in sorted(files)
                           for h in (name.encode(), files[name]))
    import hashlib
    return {"files": {name: "sha256:" + hashlib.sha256(blob).hexdigest()
                      for name, blob in files.items()},
            "bundle": "sha256:" + hashlib.sha256(overall_src).hexdigest()}


def build_bundle(self) -> bytes:
    data = _bundle_data(self)
    data["manifest"]["file_hashes"] = determinism_manifest(data)["files"]
    files = {f"citeweave/{name}.json": _dump(data[name])
             for name in ("manifest", *DATA_NAMES)}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name in sorted(files):
            info = zipfile.ZipInfo(name, date_time=_ZIP_DATE)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            zf.writestr(info, files[name])
    return buf.getvalue()


def verify_anchors(self) -> list[dict[str, Any]]:
    """Re-check every anchor against the stored (possibly re-wrapped) text."""
    data = _bundle_data(self)
    raw_by_version = {v["id"]: v["raw_text"] for v in data["versions"]}
    report = []
    for anchor in data["anchors"]:
        raw = raw_by_version[anchor["version_id"]]
        status = anchor_status(anchor["raw_fingerprint"],
                               anchor["norm_fingerprint"], raw)
        quote_ok = raw[anchor["start"]:anchor["end"]] == anchor["quote"]
        report.append({"citation_id": anchor["citation_id"],
                       "status": status,
                       "offset_quote_intact": quote_ok})
    return report
