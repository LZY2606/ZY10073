"""Duplicate requests: identical calls must not mutate or duplicate."""
from .conftest import ALPHA_2020


def test_same_content_same_label_is_replayed(svc):
    first = svc.import_version(**ALPHA_2020)
    assert first["status"] == 201
    second = svc.import_version(**ALPHA_2020)
    assert second["replayed"] is True
    assert second["version_id"] == first["version_id"]
    versions = svc.list_versions("ALPHA")
    assert len(versions) == 1


def test_idempotency_key_replays_cached_result(svc):
    payload = {**ALPHA_2020, "idempotency_key": "req-123"}
    first = svc.import_version(**payload)
    second = svc.import_version(**payload)
    assert first["version_id"] == second["version_id"]
    assert second["replayed"] is True
    assert len(svc.list_versions("ALPHA")) == 1


def test_same_label_different_text_conflicts_not_overwrite(svc):
    import pytest
    from citeweave.models import Conflict
    svc.import_version(**ALPHA_2020)
    changed = {**ALPHA_2020,
               "raw_text": ALPHA_2020["raw_text"].replace("制定本法", "制定本办法")}
    with pytest.raises(Conflict) as exc:
        svc.import_version(**changed)
    assert exc.value.base != exc.value.current
    # Original raw text survives untouched.
    version = svc.get_version(1)
    assert "制定本法" in version["raw_text"]
