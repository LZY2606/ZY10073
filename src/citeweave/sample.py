"""Optional sample loader: each *.json file describes one version."""
from __future__ import annotations

import json
from pathlib import Path

from .service import CiteWeaveService


def seed_directory(db, directory: Path) -> int:
    service = CiteWeaveService(db)
    count = 0
    for path in sorted(directory.glob("*.json")):
        spec = json.loads(path.read_text(encoding="utf-8"))
        spec.setdefault("idempotency_key", f"sample:{path.stem}")
        service.import_version(**spec)
        count += 1
    return count
