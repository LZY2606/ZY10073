"""Stable signatures/anchors shared by diff, snapshots and exports."""
from __future__ import annotations

import hashlib
import json
from typing import Any

from .textutils import normalize_ws


def citation_signature(*, code_label: str | None, article_num: str | None,
                        kind: str, text: str,
                        descriptor: dict[str, Any]) -> str:
    """Version-independent identity for the same citation across versions.

    Uses normalised quote text plus descriptor; survives rewrapping.
    """
    payload = {
        "code": code_label,
        "article": article_num,
        "kind": kind,
        "text": normalize_ws(text),
    }
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))
    return "sig:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()[:24]
