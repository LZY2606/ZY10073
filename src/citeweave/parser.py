"""Structural parser and explicit-citation extractor.

Everything here is deterministic and offset based.  The parser never resolves
targets -- it only recognises structure and emits *candidate descriptors*.
Resolution (which candidate version/unit is meant) lives in
:mod:`citeweave.resolver`, so re-parse after a user decision is cheap and
auditable.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .textutils import cn_to_int

CHAPTER_KIND = {"章": "chapter", "节": "section", "条": "article_marker"}
CHAPTER_LEVEL = {"chapter": 1, "section": 2}

# "第一章 总则" / "第十二条之一" (Japanese-style) / "第12条"
_HEAD_RE = re.compile(
    r"^[ 　]*第\s*([0-9]+|[一二三四五六七八九十百千零〇两]+)\s*"
    r"(章|节|条)\s*(之一|之二|之三)?\s*[ 　、，,：:．.]?\s*(.*)$"
)
_ITEM_RE = re.compile(r"^[ 　]*[（(]\s*([一二三四五六七八九十百]+)\s*[)）]\s*(.*)$")

# Citations.  Order matters only for reporting; spans never overlap.
_CITE_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("cross_doc", re.compile(
        r"《([^》\n]{1,40})》第\s*([0-9]+|[一二三四五六七八九十百千零〇两]+)\s*条"
        r"(?:第\s*([0-9]+|[一二三四五六七八九十]+)\s*款)?"
        r"(?:第\s*([0-9]+|[一二三四五六七八九十]+)\s*项)?")),
    ("article", re.compile(
        r"第\s*([0-9]+|[一二三四五六七八九十百千零〇两]+)\s*条"
        r"(?:第\s*([0-9]+|[一二三四五六七八九十]+)\s*款)?"
        r"(?:第\s*([0-9]+|[一二三四五六七八九十]+)\s*项)?")),
    ("prev_paragraph", re.compile(r"前\s*款")),
    ("this_article", re.compile(r"本\s*条")),
    ("prev_article", re.compile(r"前\s*条")),
    ("this_chapter", re.compile(r"本\s*(章|节)")),
]
_SENT_SPLIT_RE = re.compile(r"(?<=[。；；!?！？;])")


@dataclass
class Unit:
    key: str                 # chapter|article|paragraph|item
    num: str | None          # normalized number ("12" / "3" / None)
    kind: str                # chapter / section / article / paragraph / item
    title: str
    start: int
    end: int
    level: int = 0
    ordinal: int = 0
    article_num: str | None = None
    path: tuple[str, ...] = ()


@dataclass
class Citation:
    kind: str
    text: str
    start: int
    end: int
    sentence: str
    sent_start: int
    unit_path: tuple[str, ...]
    article_num: str | None
    paragraph_idx: int | None
    # Raw descriptor: {"title","article","paragraph","item", ...}
    descriptor: dict[str, object] = field(default_factory=dict)


@dataclass
class ParseResult:
    units: list[Unit]
    citations: list[Citation]
    raw: str
    article_count: int


def _norm_num(token: str | None) -> str | None:
    if token is None:
        return None
    if token.isdigit():
        return str(int(token))
    value = cn_to_int(token)
    return None if value is None else str(value)


def _head(line: str) -> tuple[str, str, str | None, str] | None:
    match = _HEAD_RE.match(line)
    if not match:
        return None
    return match.group(2), match.group(1), match.group(3), match.group(4).strip()


def parse_document(raw: str) -> ParseResult:
    """Split into units and extract citations with absolute offsets."""
    lines: list[tuple[int, str]] = []
    cursor = 0
    for line in raw.splitlines(keepends=True):
        lines.append((cursor, line))
        cursor += len(line)

    units: list[Unit] = []
    citations: list[Citation] = []
    stack: list[Unit] = []          # chapter / section stack
    current_article: Unit | None = None
    article_seq = 0
    para_seq = 0
    ordinal = 0

    def push_unit(kind: str, num: str | None, title: str,
                  start: int, end: int, level: int = 0) -> Unit:
        nonlocal ordinal
        while stack and level and stack[-1].level >= level:
            stack.pop()
        path = tuple(u.key for u in stack)
        unit = Unit(
            key=f"{kind}:{num if num is not None else '#' + str(ordinal + 1)}",
            num=num, kind=kind, title=title, start=start, end=end,
            level=level, ordinal=ordinal, article_num=None, path=path)
        ordinal += 1
        units.append(unit)
        if level:
            stack.append(unit)
        return unit

    para_buf: list[str] = []
    para_start = -1
    para_line_start: int | None = None
    article_body_start = 0
    article_body_lines = 0

    def flush_paragraph(end: int) -> None:
        nonlocal para_buf, para_start, para_seq
        if not para_buf:
            return
        text = "".join(para_buf).strip()
        if text:
            para_seq += 1
            p = push_unit("paragraph", None, text[:24], para_start, end)
            p.article_num = current_article.num if current_article else None
        para_buf = []
        para_start = -1

    def close_article(end: int) -> None:
        nonlocal current_article, para_seq, article_body_lines, article_body_start
        if current_article is None:
            return
        flush_paragraph(end)
        current_article.end = end
        current_article = None
        para_seq = 0

    for line_start, line in lines:
        stripped = line.strip()
        head = _head(stripped)
        item = _ITEM_RE.match(stripped)
        if head and head[0] in ("章", "节"):
            close_article(line_start)
            kind = CHAPTER_KIND[head[0]]
            num = _norm_num(head[1])
            level = CHAPTER_LEVEL[kind]
            title = f"第{head[1]}{head[0]} {head[3]}".strip()
            ch = push_unit(kind, num, title, line_start,
                           line_start + len(line), level)
            article_body_lines = 0
            continue
        if head and head[0] == "条":
            close_article(line_start)
            article_seq += 1
            num = _norm_num(head[1])
            suffix = head[2] or ""
            num_key = f"{num}{suffix}" if suffix else num
            art = push_unit("article", num_key,
                            stripped[:40], line_start,
                            line_start + len(line), level=0)
            art.article_num = num_key
            current_article = art
            article_body_start = line_start + len(stripped)
            article_body_lines = 0
            continue
        if current_article is not None and item:
            flush_paragraph(line_start)
            num = _norm_num(item.group(1))
            it = push_unit("item", num, item.group(2).strip()[:24],
                           line_start, line_start + len(line))
            it.article_num = current_article.num
            continue
        if stripped and current_article is not None:
            if not para_buf:
                para_start = line_start
            para_buf.append(line)

    end_pos = len(raw)
    close_article(end_pos)

    # Fix end offsets of enclosing chapters/sections.
    for idx, unit in enumerate(units):
        if unit.kind in ("chapter", "section"):
            nxt = next((u.start for u in units[idx + 1:]
                        if u.kind in ("chapter", "section")
                        and u.level <= unit.level), end_pos)
            unit.end = nxt

    # ---------- citations ----------
    articles = [u for u in units if u.kind == "article"]
    for art_index, art in enumerate(articles):
        art_end = (articles[art_index + 1].start
                   if art_index + 1 < len(articles) else end_pos)
        body = raw[art.start:art_end]
        body_start = art.start
        para_idx, head_skip_end = _scan_article_body(raw, art.start, art_end)
        for m in _iter_citation_matches(body):
            kind, match = m
            if body_start + match.start() < head_skip_end:
                continue  # article's own numbering marker, not a citation
            abs_start = body_start + match.start()
            abs_end = body_start + match.end()
            sent_text, s_start, s_end = _sentence_span(body, match)
            groups = match.groups()
            descriptor: dict[str, object] = {"kind": kind}
            if kind == "cross_doc":
                descriptor.update({
                    "title": groups[0],
                    "article": _norm_num(groups[1]),
                    "paragraph": _norm_num(groups[2]),
                    "item": _norm_num(groups[3]),
                })
            elif kind == "article":
                descriptor.update({
                    "article": _norm_num(groups[0]),
                    "paragraph": _norm_num(groups[1]),
                    "item": _norm_num(groups[2]),
                })
            elif kind == "this_chapter":
                descriptor["scope"] = "chapter" if match.group(0)[1] == "章" else "section"
            citations.append(Citation(
                kind=kind, text=match.group(0),
                start=abs_start, end=abs_end,
                sentence=sent_text,
                sent_start=body_start + s_start,
                unit_path=(art.key,),
                article_num=art.num,
                paragraph_idx=_paragraph_at(para_idx, abs_start),
                descriptor=descriptor))

    return ParseResult(units=units, citations=citations, raw=raw,
                       article_count=article_seq)


def _scan_article_body(raw, art_start, art_end):
    """Paragraph marks and end offset of the article heading marker.

    Chinese style places paragraph one on the heading line; Japanese style
    puts the heading alone.  Only non-item content lines count as paragraphs.
    """
    marks = []
    offset = art_start
    count = 0
    head_skip_end = art_start
    first = True
    heading_only_line = False
    for line in raw[art_start:art_end].splitlines(keepends=True):
        stripped = line.strip()
        lead = len(line) - len(line.lstrip())
        content_start = offset
        if first:
            first = False
            match = _HEAD_RE.match(stripped)
            if match:
                # End of "第N条" marker; body content after it is paragraph 1
                # (Chinese style).  A heading-only line (Japanese style) is
                # not a paragraph at all.
                head_skip_end = offset + lead + match.end(2)
                tail = stripped[match.end():].strip()
                heading_only_line = not tail
                content_start = head_skip_end
        head = _head(stripped)
        if stripped and head and head[0] in ("章", "节"):
            offset += len(line)
            continue
        if stripped and not _ITEM_RE.match(stripped):
            if heading_only_line:
                heading_only_line = False
                # heading-only first line: nothing to count
            elif raw[content_start:offset + len(line)].strip():
                count += 1
                marks.append((content_start, count))
        offset += len(line)
    return marks, head_skip_end


def _paragraph_at(marks: list[tuple[int, int]], offset: int) -> int | None:
    current = None
    for mark, idx in marks:
        if mark <= offset:
            current = idx
        else:
            break
    return current


def _iter_citation_matches(body: str):
    all_matches: list[tuple[int, int, str, re.Match[str]]] = []
    for kind, pattern in _CITE_PATTERNS:
        for match in pattern.finditer(body):
            all_matches.append((match.start(), match.end(), kind, match))
    all_matches.sort(key=lambda x: (x[0], -(x[1] - x[0])))
    busy: list[tuple[int, int]] = []
    for start, end, kind, match in all_matches:
        if any(start < e and end > s for s, e in busy):
            continue
        busy.append((start, end))
        yield kind, match


def _sentence_span(body: str, match: re.Match[str]) -> tuple[str, int, int]:
    start = 0
    for piece in _SENT_SPLIT_RE.split(body[:match.start()]):
        start += len(piece)
    remainder = body[start:]
    parts = _SENT_SPLIT_RE.split(remainder)
    sentence = parts[0]
    return sentence.strip(), start, start + len(sentence)
