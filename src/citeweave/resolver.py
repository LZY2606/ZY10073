"""Candidate generation and selection for explicit citations.

Selection is deterministic and fully explained: every candidate keeps a
``basis`` record and the resolution stores why the winner was picked.  The
resolver never substitutes a newer version for a historical one -- when more
than one version can answer a citation the result is
``cross_version_conflict`` until a human decides.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from . import PARSER_VERSION


@dataclass
class VUnit:
    unit_id: int
    version_id: int
    key: str
    kind: str
    num: str | None
    title: str
    start: int
    end: int
    level: int
    ordinal: int
    article_num: str | None
    path: tuple[str, ...]


@dataclass
class VVersion:
    version_id: int
    document_id: int
    label: str
    state: str
    effective_date: str
    end_date: str | None
    title: str
    units: list[VUnit] = field(default_factory=list)


@dataclass
class VCitation:
    citation_id: int
    version_id: int
    source_article_num: str | None
    paragraph_idx: int | None
    kind: str
    descriptor: dict[str, Any]


@dataclass
class Candidate:
    target_version_id: int | None
    target_unit_id: int | None
    target_desc: str
    status: str
    score: float
    basis: dict[str, Any]


def _find_article(version: VVersion, num: str) -> VUnit | None:
    return next((u for u in version.units
                 if u.kind == "article" and u.article_num == num), None)


def _paragraph_unit(version: VVersion, article: VUnit,
                    idx: str | int | None) -> VUnit:
    if idx is None:
        return article
    idx = int(idx)
    paras = [u for u in version.units
             if u.kind == "paragraph" and u.article_num == article.article_num]
    if 0 < idx <= len(paras):
        return paras[idx - 1]
    return article


def _item_unit(version: VVersion, article: VUnit,
               item_num: str | None) -> VUnit:
    if item_num is None:
        return article
    item = next((u for u in version.units
                 if u.kind == "item" and u.article_num == article.article_num
                 and u.num == item_num), None)
    return item or article


def _version_status(version: VVersion, target_date: str | None) -> str:
    """Lifecycle state as seen at ``target_date`` (default: citation era)."""
    if version.state == "repealed":
        return "repealed"
    if target_date is None:
        return "valid"
    if version.effective_date > target_date:
        return "future"
    if version.end_date and version.end_date <= target_date:
        if version.state == "repealed":
            return "repealed"
        return "superseded"
    return "valid"


def _enclosing_chapter(version: VVersion, article: VUnit) -> VUnit | None:
    candidates = [u for u in version.units
                  if u.kind in ("chapter", "section")
                  and u.start <= article.start and u.end >= article.end]
    return candidates[-1] if candidates else None


def _article_by_ordinal(version: VVersion, ordinal: int) -> VUnit | None:
    arts = [u for u in version.units if u.kind == "article"]
    if 0 <= ordinal < len(arts):
        return arts[ordinal]
    return None


def build_candidates(citation: VCitation, source_version: VVersion,
                     versions: list[VVersion],
                     docs: dict[int, str]) -> list[Candidate]:
    """All plausible targets ordered best-first; nothing is dropped."""
    desc = citation.descriptor
    out: list[Candidate] = []
    same = source_version
    art_num = citation.source_article_num
    source_article = (_find_article(same, art_num)
                      if art_num is not None else None)
    own_order = [u for u in same.units if u.kind == "article"]
    own_idx = next((i for i, u in enumerate(own_order)
                    if u is source_article), -1)

    def add(unit: VUnit | None, version: VVersion, score: float,
            status: str, reason: str, **extra: Any) -> None:
        if unit is None:
            out.append(Candidate(
                None, None, reason, status, score,
                {"reason": reason, "version_label": version.label, **extra}))
            return
        locator = (f"{version.label} / {unit.kind}"
                   f" {unit.num or unit.ordinal + 1}")
        out.append(Candidate(
            version.version_id, unit.unit_id, locator, status, score,
            {"reason": reason, "version_label": version.label,
             "unit_key": unit.key, **extra}))

    if citation.kind == "prev_paragraph":
        if citation.paragraph_idx and citation.paragraph_idx > 1:
            paras = [u for u in same.units
                     if u.kind == "paragraph"
                     and u.article_num == source_article.article_num]
            add(paras[citation.paragraph_idx - 2], same, 100, "valid",
                "previous paragraph of same article")
        elif own_idx > 0:
            prev = own_order[own_idx - 1]
            last_p = [u for u in same.units
                      if u.kind == "paragraph"
                      and u.article_num == prev.article_num][-1:]
            add(last_p[0] if last_p else prev, same, 90, "valid",
                "last paragraph of previous article")
        else:
            out.append(Candidate(None, None, "无前款", "missing", 0,
                                 {"reason": "first article has no predecessor"}))

    elif citation.kind == "this_article":
        add(source_article, same, 100, "valid", "same article (本条)")

    elif citation.kind == "prev_article":
        add(_article_by_ordinal(same, own_idx - 1), same, 100, "valid",
            "previous article (前条)")

    elif citation.kind == "this_chapter":
        add(_enclosing_chapter(same, source_article), same, 100, "valid",
            f"enclosing {desc.get('scope', 'chapter')} (本{desc.get('scope')})")

    elif citation.kind in ("article", "cross_doc"):
        wanted_doc_title = desc.get("title")
        article_num = desc["article"]
        para_num = desc.get("paragraph")
        item_num = desc.get("item")
        versions_to_scan = versions
        if citation.kind == "cross_doc":
            matched = [v for v in versions
                       if v.document_id != source_version.document_id
                       and v.title == wanted_doc_title]
            if not matched:
                out.append(Candidate(
                    None, None, f"《{wanted_doc_title}》第{article_num}条",
                    "missing", 0,
                    {"reason": "cited document title not imported",
                     "title": wanted_doc_title}))
                return out
            versions_to_scan = matched
        else:
            versions_to_scan = [v for v in versions
                                if v.document_id == source_version.document_id]

        for version in versions_to_scan:
            article = _find_article(version, article_num)
            if article is None:
                out.append(Candidate(
                    None, None,
                    f"{version.label} 第{article_num}条", "missing", 0,
                    {"reason": "article number absent in this version",
                     "version_label": version.label, "article": article_num}))
                continue
            target = _item_unit(
                version, _paragraph_unit(version, article, para_num), item_num)
            if version.version_id == same.version_id:
                score, status = 100, "valid"
                reason = "same version, same document"
            elif version.state == "repealed":
                score, status, reason = 25, "repealed", "repealed version"
            elif version.state == "superseded":
                score, status, reason = 40, "valid", "historical version"
            elif version.state == "adopted":
                score, status, reason = 55, "future", "adopted, not in force"
            else:
                score, status, reason = 70, "valid", "other live version"
            if para_num or item_num:
                reason += "; paragraph/item locator"
            add(target, version, score, status, reason,
                article=article_num, paragraph=para_num, item=item_num)

    out.sort(key=lambda c: (-c.score, c.target_desc))
    # Stable re-rank.
    for idx, candidate in enumerate(out):
        candidate.basis["rank"] = idx
    return out


def _versions_of(versions, version_id):
    return [v for v in versions if v.version_id == version_id]


def _doc_of(versions, version_id):
    for v in versions:
        if v.version_id == version_id:
            return v.document_id
    return None


def resolve(citation: VCitation, source_version: VVersion,
            versions: list[VVersion], docs: dict[int, str],
            hint: dict[str, Any] | None = None
            ) -> tuple[list[Candidate], str, dict[str, Any]]:
    """Pick the winning candidate or explain why none is authoritative."""
    candidates = build_candidates(citation, source_version, versions, docs)
    if hint:
        target_vid = hint.get("target_version_id")
        for idx, candidate in enumerate(candidates):
            if candidate.target_version_id == target_vid and (
                    hint.get("target_unit_id") in (None,
                                                   candidate.target_unit_id)):
                candidate.score += 1000
                candidate.basis["decision_boost"] = True
        candidates.sort(key=lambda c: (-c.score, c.target_desc))
        for idx, candidate in enumerate(candidates):
            candidate.basis["rank"] = idx

    real = [c for c in candidates if c.target_unit_id is not None]
    basis: dict[str, Any] = {
        "parser_version": PARSER_VERSION,
        "candidate_count": len(candidates),
        "real_target_count": len(real),
    }
    if not real:
        only_missing = candidates and all(c.status == "missing"
                                          for c in candidates)
        status = "missing" if only_missing else "missing"
        basis["why"] = "no imported unit matches the descriptor"
        return candidates, status, basis

    winner = real[0]
    # Real targets spread across different versions of the same document
    # mean the same number points at different texts.  A same-version hit is
    # unambiguous; otherwise the human must pick the era-specific target.
    same_doc_versions = {
        c.target_version_id for c in real
        if c.target_version_id
        and next((v for v in _versions_of(versions, c.target_version_id)),
                 None)
        and _doc_of(versions, c.target_version_id)
        == source_version.document_id}
    explicit = citation.kind == "article"
    # A citation whose own article quotes the same number is self-referential
    # (rare) and resolved locally; ordinary same-number cites are flagged as
    # soon as another version text of the same document answers them.
    own_article = citation.source_article_num
    self_ref = winner.basis.get("article") == own_article and (
        winner.target_version_id == source_version.version_id)
    if (explicit and not hint and len(same_doc_versions) > 1
            and not self_ref and winner.status != "repealed"):
        status = "cross_version_conflict"
        basis["why"] = ("article exists in multiple version texts; same "
                        "number maps to different wording")
        basis["competing_versions"] = sorted(same_doc_versions)
    elif winner.status == "repealed":
        status = "repealed"
        basis["why"] = "best candidate lives in a repealed version"
    elif winner.status == "future":
        status = "cross_version_conflict"
        basis["why"] = "target only exists in a not-yet-effective version"
    elif len(real) > 1 and real[0].score == real[1].score and not hint:
        status = "ambiguous"
        basis["why"] = "two candidates tie; researcher input required"
    else:
        status = "resolved"
        basis["why"] = winner.basis.get("reason", "unique best candidate")
    basis["winner_desc"] = winner.target_desc
    return candidates, status, basis
