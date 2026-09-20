"""Deterministic export + anchor survival across rewrapping."""
import io
import zipfile

from citeweave.textutils import anchor_status, content_fingerprint, raw_fingerprint
from .conftest import ALPHA_2020, ALPHA_2023, BETA_2019


def test_bundle_bytes_are_deterministic(seeded):
    first = seeded.build_bundle()
    second = seeded.build_bundle()
    assert first == second
    with zipfile.ZipFile(io.BytesIO(first)) as zf:
        assert zf.namelist() == sorted(zf.namelist())
        import json
        manifest = json.loads(zf.read("citeweave/manifest.json"))
        assert manifest["parser_version"].startswith("citeweave-parser/")
        # Every listed data-file hash matches actual content.
        import hashlib
        for name, digest in manifest["file_hashes"].items():
            assert not name.endswith("manifest.json")
            actual = "sha256:" + hashlib.sha256(zf.read(name)).hexdigest()
            assert actual == digest
        anchors = json.loads(zf.read("citeweave/anchors.json"))
        assert anchors, "anchors must be exported"
        assert {"start", "end", "quote", "signature", "raw_fingerprint",
                "norm_fingerprint"} <= set(anchors[0])


def test_anchor_distinguishes_reflow_from_content_change():
    raw = ALPHA_2020["raw_text"]
    reflowed = "  ".join(raw.splitlines()) + "\n"
    changed = raw.replace("本市", "本省")
    assert anchor_status(raw_fingerprint(raw),
                         content_fingerprint(raw), raw) == "exact"
    assert anchor_status(raw_fingerprint(raw),
                         content_fingerprint(raw), reflowed) == "reflowed"
    assert anchor_status(raw_fingerprint(raw),
                         content_fingerprint(raw), changed) == "failed"


def test_verify_anchors_reports_intact(seeded):
    report = seeded.verify_anchors()
    assert report
    assert all(row["status"] == "exact"
               and row["offset_quote_intact"] for row in report)


def test_graph_history_does_not_substitute_newest(seeded):
    old = seeded.graph_at("2021-01-01")
    new = seeded.graph_at("2024-01-01")
    old_targets = {e["text"]: e["target_desc"] for e in old["edges"]}
    new_targets = {e["text"]: e["target_desc"] for e in new["edges"]}
    # 2020 graph edges must point at the 2020 version, never the 2023 text.
    assert old_targets["前款"].startswith("甲法-2020")
    assert new_targets["前款"].startswith("甲法-2023")
    # 第十五条 only exists in 2023: present later, absent historically.
    assert any(e["text"] == "第十五条" for e in new["edges"])
    assert not any(e["text"] == "第十五条" for e in old["edges"])


def test_date_diff_categorises_added_removed_redirected(seeded):
    diff = seeded.date_diff("2021-01-01", "2024-01-01")
    added = {e["text"] for e in diff["added"]}
    removed = {e["text"] for e in diff["removed"]}
    assert "第十五条" in added
    assert "前款" not in added and "前款" not in removed
    redir = {r["from"]["text"] for r in diff["redirected"]}
    assert {"前款", "前条", "本章"} & redir


def test_cross_document_paragraph_locator(seeded):
    detail = None
    for edge in seeded.graph_at("2024-01-01")["edges"]:
        if edge["text"].startswith("《乙法》"):
            detail = seeded.citation_detail(edge["id"])
    assert detail is not None
    winning = next(c for c in detail["candidates"]
                   if c["id"] == detail["resolution"]["selected_candidate_id"])
    assert winning["target_desc"].startswith("乙法-2019")
    assert winning["basis"].get("paragraph") == "2"
