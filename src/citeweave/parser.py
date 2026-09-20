from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any

PARSER_VERSION = "citeweave-parser-1.0.0"

_CHINESE_DIGITS = {
    "零": 0,
    "〇": 0,
    "一": 1,
    "二": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}
_META_RE = re.compile(r"^<!--\s*(title|doc_key|version_label|effective|repealed)\s*:\s*(.*?)\s*-->$")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
_CHAPTER_RE = re.compile(r"^(第[零〇一二三四五六七八九十百0-9]+[编章节])\s*(.*)$")
_ARTICLE_RE = re.compile(r"^(第[零〇一二三四五六七八九十百0-9]?[0-9]*条)\s*(.*)$")
_ITEM_RE = re.compile(r"^[（(]([零〇一二三四五六七八九十百0-9]+)[)）]\s*(.*)$")
_SENTENCE_SPLIT_RE = re.compile(r".+?[。！？；;](?:[”’\"])?|.+$", re.S)
_ARTICLE_REF_RE = re.compile(
    r"第(?P<article>[零〇一二三四五六七八九十百0-9]+)条"
    r"(?:第(?P<paragraph>[零〇一二三四五六七八九十百0-9]+)款)?"
    r"(?:第[（(]?(?P<item>[零〇一二三四五六七八九十百0-9]+)[)）]?项)?"
)
_SENTENCE_TERMINATORS = "。！？；;"


def fingerprint(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def short_hash(text: str) -> str:
    return fingerprint(text)[:12]


def chinese_to_int(value: str) -> int | None:
    value = value.strip()
    if value.isdigit():
        return int(value)
    total = 0
    section = 0
    number = 0
    unit_seen = False
    units = {"十": 10, "百": 100}
    for char in value:
        if char in _CHINESE_DIGITS:
            number = _CHINESE_DIGITS[char]
        elif char in units:
            unit = units[char]
            section += (number or 1) * unit
            number = 0
            unit_seen = True
        else:
            return None
    result = section + number
    if not unit_seen and not value.isdigit():
        # "一一" is intentionally unsupported; "十一" is handled above.
        pass
    return result if result else None


def normalize_text(text: str) -> str:
    lines = [line.strip() for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    return "\n".join(lines).strip()


def _node_id(*parts: str) -> str:
    return "n_" + short_hash("\x1f".join(parts))


def _citation_id(version_key: str, semantic_source: str, quote: str, start: int) -> str:
    return "cit_" + short_hash("\x1f".join([version_key, semantic_source, quote, str(start)]))


def _citation_semantic_key(
    doc_key: str, source: dict[str, Any], quote: str, occurrences: dict[tuple[str, str], int]
) -> str:
    key = (source["locator"], quote)
    occurrences[key] = occurrences.get(key, 0) + 1
    return f"{doc_key}|{source['locator']}|{quote}#occ{occurrences[key]}"


def _split_sentences(paragraph: str) -> list[tuple[int, int, str]]:
    result: list[tuple[int, int, str]] = []
    for match in _SENTENCE_SPLIT_RE.finditer(paragraph.strip()):
        sentence = match.group(0)
        if not sentence:
            continue
        start = paragraph.find(sentence, match.start())
        result.append((start, start + len(sentence), sentence))
    return result


@dataclass(frozen=True)
class ParseResult:
    raw_text: str
    normalized_text: str
    clauses: list[dict[str, Any]]
    citations: list[dict[str, Any]]

    def as_dict(self) -> dict[str, Any]:
        return {
            "raw_text": self.raw_text,
            "normalized_text": self.normalized_text,
            "clauses": self.clauses,
            "citations": self.citations,
        }


def parse_document(
    raw_text: str,
    *,
    doc_key: str,
    version_label: str,
    source_fingerprint: str | None = None,
) -> ParseResult:
    normalized = normalize_text(raw_text)
    version_key = f"{doc_key}@{version_label}"
    clauses: list[dict[str, Any]] = []
    citations: list[dict[str, Any]] = []
    chapter: dict[str, str | int | None] | None = None
    article: dict[str, Any] | None = None
    paragraph_count = 0
    paragraph_keys: list[str] = []
    citation_occurrences: dict[tuple[str, str], int] = {}

    def add_clause(
        level: str,
        number: str | None,
        title: str,
        text: str,
        *,
        parent_key: str | None,
        chapter_key: str | None,
        article_key: str | None,
        ordinal: int,
        prev_paragraph_key: str | None = None,
        number_value: int | None = None,
    ) -> dict[str, Any]:
        locator_parts = [part for part in [chapter_key, article_key, number] if part]
        if level == "chapter":
            locator = number or title
            semantic_key = f"{doc_key}|{locator}"
        elif level == "article":
            locator = number or title
            semantic_key = f"{doc_key}|{chapter_key or ''}|{locator}"
        elif level == "paragraph":
            locator = f"{article_key}#第{number_value}款"
            semantic_key = f"{doc_key}|{chapter_key or ''}|{article_key}|p{number_value}"
        elif level == "item":
            locator = f"{article_key}#第{paragraph_count}款#{number}"
            semantic_key = f"{doc_key}|{chapter_key or ''}|{article_key}|p{paragraph_count}|{number}"
        else:
            locator = title
            semantic_key = f"{doc_key}|{title}"
        clause_text = text.strip()
        normalized_clause = normalize_text(clause_text)
        node = {
            "id": _node_id(version_key, semantic_key),
            "semantic_key": semantic_key,
            "locator": locator,
            "level": level,
            "number": number,
            "number_value": number_value,
            "heading": title.strip(),
            "text": clause_text,
            "normalized_text": normalized_clause,
            "parent_semantic_key": parent_key,
            "chapter_semantic_key": chapter_key,
            "article_semantic_key": article_key,
            "previous_paragraph_semantic_key": prev_paragraph_key,
            "ordinal": ordinal,
            "text_fingerprint": fingerprint(clause_text),
            "normalized_fingerprint": fingerprint(normalized_clause),
        }
        clauses.append(node)
        return node

    ordinal = 0
    for line_number, original_line in enumerate(normalized.split("\n"), start=1):
        line = original_line.strip()
        if not line:
            continue
        meta = _META_RE.match(line)
        if meta:
            continue
        heading = _HEADING_RE.match(line)
        body = line
        if heading:
            body = heading.group(2).strip()
        chapter_match = _CHAPTER_RE.match(body)
        article_match = _ARTICLE_RE.match(body)
        item_match = _ITEM_RE.match(body)
        if chapter_match:
            number, rest = chapter_match.groups()
            chapter = {
                "key": f"{doc_key}|{number}",
                "number": number,
                "value": chinese_to_int(number[1:-1]),
            }
            article = None
            paragraph_count = 0
            paragraph_keys = []
            ordinal += 1
            add_clause(
                "chapter",
                number,
                rest or number,
                rest or number,
                parent_key=f"{doc_key}|document",
                chapter_key=number,
                article_key=None,
                ordinal=ordinal,
                number_value=chapter["value"],
            )
            continue
        if article_match:
            number, rest = article_match.groups()
            article_key = f"{doc_key}|{(chapter or {}).get('number') or ''}|{number}"
            article = {"key": article_key, "number": number, "value": chinese_to_int(number[1:-1])}
            paragraph_count = 0
            paragraph_keys = []
            ordinal += 1
            article_node = add_clause(
                "article",
                number,
                rest,
                rest,
                parent_key=chapter["key"] if chapter else f"{doc_key}|document",
                chapter_key=(chapter or {}).get("number"),
                article_key=number,
                ordinal=ordinal,
                number_value=article["value"],
            )
            paragraph_text = rest
            if paragraph_text:
                previous = paragraph_keys[-1] if paragraph_keys else None
                paragraph_count += 1
                ordinal += 1
                pnode = add_clause(
                    "paragraph",
                    f"第{paragraph_count}款",
                    number,
                    paragraph_text,
                    parent_key=article_node["semantic_key"],
                    chapter_key=(chapter or {}).get("number"),
                    article_key=number,
                    ordinal=ordinal,
                    prev_paragraph_key=previous,
                    number_value=paragraph_count,
                )
                paragraph_keys.append(pnode["semantic_key"])
                source_node = pnode
            if not paragraph_text:
                continue
        else:
            if item_match and article is not None:
                raw_number, rest = item_match.groups()
                number = f"第（{raw_number}）项"
                ordinal += 1
                source_node = add_clause(
                    "item",
                    number,
                    number,
                    rest,
                    parent_key=paragraph_keys[-1] if paragraph_keys else article["key"],
                    chapter_key=(chapter or {}).get("number"),
                    article_key=article["number"],
                    ordinal=ordinal,
                    number_value=chinese_to_int(raw_number),
                )
                source_paragraph_key = paragraph_keys[-1] if paragraph_keys else source_node["semantic_key"]
            else:
                if article is None:
                    ordinal += 1
                    title_node = add_clause(
                        "paragraph",
                        None,
                        body[:40],
                        body,
                        parent_key=chapter["key"] if chapter else f"{doc_key}|document",
                        chapter_key=(chapter or {}).get("number"),
                        article_key=None,
                        ordinal=ordinal,
                    )
                    source_node = title_node
                    source_paragraph_key = title_node["semantic_key"]
                else:
                    previous = paragraph_keys[-1] if paragraph_keys else None
                    paragraph_count += 1
                    ordinal += 1
                    source_node = add_clause(
                        "paragraph",
                        f"第{paragraph_count}款",
                        article["number"],
                        body,
                        parent_key=article["key"],
                        chapter_key=(chapter or {}).get("number"),
                        article_key=article["number"],
                        ordinal=ordinal,
                        prev_paragraph_key=previous,
                        number_value=paragraph_count,
                    )
                    paragraph_keys.append(source_node["semantic_key"])
                    source_paragraph_key = source_node["semantic_key"]

        for start, end, sentence in _split_sentences(source_node["text"]):
            for match in _ARTICLE_REF_RE.finditer(sentence):
                quote = match.group(0)
                citations.append(
                    {
                        "id": _citation_id(version_key, source_node["semantic_key"], quote, start + match.start()),
                        "semantic_key": _citation_semantic_key(
                            doc_key, source_node, quote, citation_occurrences
                        ),
                        "source_clause_id": source_node["id"],
                        "source_semantic_key": source_node["semantic_key"],
                        "sentence": sentence,
                        "quote": quote,
                        "ref_type": "article",
                        "requested_article": chinese_to_int(match.group("article")),
                        "requested_paragraph": chinese_to_int(match.group("paragraph"))
                        if match.group("paragraph")
                        else None,
                        "requested_item": chinese_to_int(match.group("item")) if match.group("item") else None,
                        "char_start": start + match.start(),
                        "char_end": start + match.end(),
                        "line_number": line_number,
                        "anchor_locator": source_node["locator"],
                        "anchor_fingerprint": source_node["text_fingerprint"],
                        "normalized_anchor_fingerprint": source_node["normalized_fingerprint"],
                    }
                )
            for positional in ("前款", "本章"):
                for match in re.finditer(positional, sentence):
                    quote = match.group(0)
                    citations.append(
                        {
                            "id": _citation_id(version_key, source_node["semantic_key"], quote, start + match.start()),
                            "semantic_key": _citation_semantic_key(
                                doc_key, source_node, quote, citation_occurrences
                            ),
                            "source_clause_id": source_node["id"],
                            "source_semantic_key": source_node["semantic_key"],
                            "sentence": sentence,
                            "quote": quote,
                            "ref_type": "previous_paragraph" if quote == "前款" else "current_chapter",
                            "requested_article": None,
                            "requested_paragraph": None,
                            "requested_item": None,
                            "char_start": start + match.start(),
                            "char_end": start + match.end(),
                            "line_number": line_number,
                            "anchor_locator": source_node["locator"],
                            "anchor_fingerprint": source_node["text_fingerprint"],
                            "normalized_anchor_fingerprint": source_node["normalized_fingerprint"],
                        }
                    )

    return ParseResult(
        raw_text=raw_text,
        normalized_text=normalized,
        clauses=clauses,
        citations=citations,
    )
