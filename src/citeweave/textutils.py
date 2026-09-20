"""Text utilities: Chinese numerals, fingerprints and offsets."""
from __future__ import annotations

import hashlib
import re
import unicodedata

_CN_DIGITS = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3,
              "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}


def cn_to_int(token: str) -> int | None:
    """Convert a legal-style Chinese numeral (<=9999) to int."""
    if not token:
        return None
    if token in ("零", "〇"):
        return 0
    total = 0
    section = 0
    number = 0
    found = False
    for ch in token:
        if ch in _CN_DIGITS:
            number = _CN_DIGITS[ch]
            found = True
        elif ch == "十":
            section += number if number else 1
            section *= 10
            number = 0
            found = True
        elif ch == "百":
            section += number * 100
            number = 0
            found = True
        elif ch == "千":
            section += number * 1000
            number = 0
            found = True
        else:
            return None
    total = section + number
    return total if found and total > 0 else None


_WS_RE = re.compile(r"\s+")


def normalize_ws(text: str) -> str:
    """NFKC + collapse whitespace.  Never mutates stored raw text."""
    return _WS_RE.sub(" ", unicodedata.normalize("NFKC", text)).strip()


def raw_fingerprint(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def content_fingerprint(text: str) -> str:
    """Whitespace-insensitive fingerprint: survives reflow/rewrapping."""
    squashed = "".join(normalize_ws(text).split())
    return "sha256-norm:" + hashlib.sha256(squashed.encode("utf-8")).hexdigest()


def anchor_status(raw_hash: str, norm_hash: str, current_raw: str) -> str:
    """Check an exported anchor against a possibly rewrapped text."""
    if raw_fingerprint(current_raw) == raw_hash:
        return "exact"
    if content_fingerprint(current_raw) == norm_hash:
        return "reflowed"
    return "failed"
