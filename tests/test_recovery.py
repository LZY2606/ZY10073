"""Process killed between stage and finalize must be recoverable."""
import os
import subprocess
import sys

from citeweave.service.core import CRASH_POINT_AFTER_STAGE
from .conftest import ALPHA_2020

HELPER = r'''
import sys
from citeweave.db import DB
from citeweave.service import CiteWeaveService
db_path, crash_version = sys.argv[1], sys.argv[2]
svc = CiteWeaveService(DB(db_path))
if crash_version:
    import os
    os.environ["CITEWEAVE_CRASH_AFTER_STAGE"] = crash_version
svc.import_version(stage="staged", **{kw})
'''


def _run_helper(db_path: str, crash_version: str = "", spec=ALPHA_2020):
    code = (
        "import os, sys\n"
        "from citeweave.db import DB\n"
        "from citeweave.service import CiteWeaveService\n"
        "from citeweave.service.core import CRASH_POINT_AFTER_STAGE\n"
        f"spec = {spec!r}\n"
        f"os.environ[CRASH_POINT_AFTER_STAGE] = {crash_version!r}\n"
        "svc = CiteWeaveService(DB(sys.argv[1]))\n"
        "svc.import_version(stage='staged', **spec)\n"
        "print('survived')\n")
    return subprocess.run([sys.executable, "-c", code, db_path],
                          capture_output=True, text=True)


def test_killed_process_leaves_staged_row_that_recovery_abandons(tmp_path):
    db_path = str(tmp_path / "c.sqlite3")
    run = _run_helper(db_path, crash_version="1")
    assert run.returncode == 17  # os._exit simulating SIGKILL

    from citeweave.db import DB
    from citeweave.service import CiteWeaveService
    db = DB(db_path)
    assert db.recover() == 1  # explicit recovery after killed process
    try:
        status = CiteWeaveService(db).recovery_status()
        assert len(status["abandoned_stages"]) == 1
        svc = CiteWeaveService(db)
        # Finalising an abandoned stage is refused; re-import works.
        import pytest
        from citeweave.models import Conflict
        with pytest.raises(Conflict):
            svc.finalize_version(1)
        # A re-import with a new label bypasses the abandoned row; here we
        # simulate the researcher retrying after the crash with a distinct
        # version label (same text is preserved, nothing is overwritten).
        retry = {**ALPHA_2020, "label": ALPHA_2020["label"] + "-retry"}
        again = svc.import_version(stage="staged", **retry)
        assert again["version_id"] == 2
        done = svc.finalize_version(2)
        assert done["stage"] == "final"
        graph = svc.graph_at("2020-06-01")
        assert any(e["text"] == "前款" for e in graph["edges"])
    finally:
        db.close()


def test_clean_staged_then_finalize_after_normal_restart(tmp_path):
    db_path = str(tmp_path / "c.sqlite3")
    run = _run_helper(db_path, crash_version="999")  # never matches
    assert "survived" in run.stdout
    from citeweave.db import DB
    from citeweave.service import CiteWeaveService
    db = DB(db_path)  # no auto-recovery: clean staged row survives
    try:
        svc = CiteWeaveService(db)
        result = svc.finalize_version(1)
        assert result["status"] == 201
        assert svc.recovery_status()["abandoned_stages"] == []
    finally:
        db.close()


def test_reopen_final_version_is_idempotent(tmp_path):
    db_path = str(tmp_path / "c.sqlite3")
    from citeweave.db import DB
    from citeweave.service import CiteWeaveService
    db = DB(db_path)
    svc = CiteWeaveService(db)
    svc.import_version(stage="staged", **ALPHA_2020)
    svc.finalize_version(1)
    assert svc.finalize_version(1)["replayed"] is True
    db.close()
