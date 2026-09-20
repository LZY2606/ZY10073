"""Illegal state jumps must be refused (not silently coerced)."""
import pytest

from citeweave.models import IllegalTransition
from .conftest import ALPHA_2020


def test_full_legal_lifecycle(svc):
    svc.import_version(**{**ALPHA_2020, "state": "adopted",
                          "effective_date": "2020-01-01"})
    assert svc.transition_version(1, "effective")["state"] == "effective"
    assert svc.transition_version(1, "superseded")["state"] == "superseded"
    assert svc.transition_version(1, "repealed")["state"] == "repealed"


def test_adopted_may_repeal_without_effective(svc):
    svc.import_version(**{**ALPHA_2020, "state": "adopted"})
    assert svc.transition_version(1, "repealed")["state"] == "repealed"


@pytest.mark.parametrize("start,bad", [
    ("adopted", "superseded"),
    ("effective", "adopted"),
    ("superseded", "effective"),
    ("repealed", "effective"),
    ("repealed", "superseded"),
])
def test_illegal_jumps(svc, start, bad):
    svc.import_version(**{**ALPHA_2020, "state": start})
    if start == "effective":
        pass
    with pytest.raises(IllegalTransition) as exc:
        svc.transition_version(1, bad)
    assert exc.value.frm == start and exc.value.to == bad
    assert svc.get_version(1)["state"] == start


def test_snapshot_publish_is_one_way(svc):
    svc.import_version(**ALPHA_2020)
    snap = svc.create_snapshot(date="2020-06-01", label="x")
    svc.publish_snapshot(snap["snapshot_id"], 1)
    with pytest.raises(IllegalTransition):
        svc.publish_snapshot(snap["snapshot_id"], 2)
