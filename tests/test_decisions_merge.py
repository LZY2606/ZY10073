"""Human decisions, optimistic conflicts and two-researcher merges."""
import json

import pytest

from citeweave.models import Conflict
from .conftest import ALPHA_2020, ALPHA_2023, BETA_2019


def _citation_id(svc, version_label: str, text: str) -> int:
    version = next(v for v in svc.list_versions("ALPHA")
                   if v["version_label"] == version_label)
    rows = svc.db.query(
        "SELECT id FROM citations WHERE version_id=? AND text=?",
        (version["id"], text))
    return rows[0]["id"]


def test_stale_write_returns_both_sides_instead_of_overwrite(seeded):
    cid = _citation_id(seeded, "甲法-2023", "前款")
    seeded.add_decision(citation_id=cid, researcher="alice",
                        action="override", target_version_id=1,
                        source_page="公报 p.12", base_rev=0)
    with pytest.raises(Conflict) as exc:
        seeded.add_decision(citation_id=cid, researcher="bob",
                            action="override", target_version_id=2,
                            source_page="汇编 p.30", base_rev=0)
    assert exc.value.current["researcher"] == "alice"
    assert exc.value.current["target_version_id"] == 1
    assert exc.value.base["rev"] == 0


def test_two_researchers_divergent_targets_both_kept(seeded):
    cid = _citation_id(seeded, "甲法-2023", "前款")
    seeded.add_decision(citation_id=cid, researcher="alice",
                        action="override", target_version_id=1,
                        source_page="p.12", base_rev=0)
    seeded.add_decision(citation_id=cid, researcher="bob",
                        action="override", target_version_id=2,
                        source_page="p.30", base_rev=1)
    detail = seeded.citation_detail(cid)
    who = {d["researcher"]: d for d in detail["decisions"]}
    assert who["alice"]["target_version_id"] == 1
    assert who["bob"]["target_version_id"] == 2

    v2023 = next(v["id"] for v in seeded.list_versions("ALPHA")
                 if v["version_label"] == "甲法-2023")
    session = seeded.create_merge_session(
        label="m", base_version_id=v2023,
        researcher_a="alice", researcher_b="bob")
    assert cid in session["divergences"]
    full = seeded.get_merge_session(session["session_id"])
    sides = full["items"][cid]
    assert {s["side"] for s in sides} == {"A", "B"}
    assert all(s["quote"] == "前款" and s["context"] for s in sides)
    assert {s["source_page"] for s in sides} == {"p.12", "p.30"}

    # Adjudicating keeps an explicit record; un-adjudicated divergences stay.
    result = seeded.merge_session(session["session_id"], base_rev=1)
    assert cid in result["divergences_kept"]
    result = seeded.merge_session(session["session_id"], base_rev=2,
                                  resolutions={str(cid): "A"})
    assert cid in result["adjudicated"]


def test_decision_triggers_reparse_of_affected_nodes(seeded):
    cid = _citation_id(seeded, "甲法-2023", "前款")
    result = seeded.add_decision(
        citation_id=cid, researcher="alice", action="version_range",
        target_version_id=1, valid_from="2020-01-01", valid_to="2023-05-31",
        base_rev=0)
    assert cid in result["reparsed"]
    # Relative citations in the same version are re-evaluated too.
    assert len(result["reparsed"]) > 1
    resolution = seeded.citation_detail(cid)["resolution"]
    assert resolution["revision"] >= 2


def test_version_range_validation(seeded):
    cid = _citation_id(seeded, "甲法-2023", "前款")
    with pytest.raises(Exception):
        seeded.add_decision(citation_id=cid, researcher="a",
                            action="version_range", target_version_id=1,
                            valid_from="2025-01-01", valid_to="2020-01-01",
                            base_rev=0)


def test_published_snapshot_is_immutable_after_later_edits(seeded):
    snap = seeded.create_snapshot(date="2021-01-01", label="freeze",
                                  researcher="alice")
    seeded.publish_snapshot(snap["snapshot_id"], 1)
    before = seeded.get_snapshot(snap["snapshot_id"])
    cid = _citation_id(seeded, "甲法-2023", "前款")
    seeded.add_decision(citation_id=cid, researcher="alice",
                        action="override", target_version_id=1, base_rev=0)
    after = seeded.get_snapshot(snap["snapshot_id"])
    assert before["body"] == after["body"]
    assert before["payload_fingerprint"] == after["payload_fingerprint"]


def test_snapshot_stale_publish_conflict(seeded):
    snap = seeded.create_snapshot(date="2021-01-01", label="x")
    with pytest.raises(Conflict) as exc:
        seeded.publish_snapshot(snap["snapshot_id"], 99)
    assert exc.value.current == 1
