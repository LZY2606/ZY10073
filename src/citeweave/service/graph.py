"""Time-valid citation graph, shortest-chain traces and date comparison."""
from __future__ import annotations

from collections import deque
from typing import Any


def _version_active(row, date: str) -> bool:
    if row["state"] == "adopted":
        return False
    if row["effective_date"] > date:
        return False
    if row["end_date"] and row["end_date"] <= date:
        return False
    return True


def graph_at(self, date: str) -> dict[str, Any]:
    """Graph visible exactly at ``date`` (never uses newest-as-substitute)."""
    with self.db.lock:
        versions = {r["id"]: dict(r) for r in self.db.query(
            "SELECT v.id,v.document_id,v.version_label,v.state,"
            "v.effective_date,v.end_date,v.title,d.code FROM versions v "
            "JOIN documents d ON d.id=v.document_id WHERE import_stage='final'")}
        units = {(r["version_id"], r["kind"], r["num"], r["article_num"]):
                 dict(r) for r in self.db.query("SELECT * FROM units")}
        citations = {r["id"]: dict(r) for r in
                     self.db.query("SELECT * FROM citations")}
        resolutions = {r["citation_id"]: dict(r) for r in
                       self.db.query("SELECT * FROM resolutions")}
        candidates = {r["id"]: dict(r) for r in
                      self.db.query("SELECT * FROM candidates")}

    nodes: dict[str, dict[str, Any]] = {}
    edges: list[dict[str, Any]] = []

    for vid, version in versions.items():
        if _version_active(version, date):
            nodes[f"v{vid}"] = {"kind": "version", "id": vid,
                                "label": version["version_label"],
                                "title": version["title"],
                                "code": version["code"]}

    for cid, citation in citations.items():
        source_vid = citation["version_id"]
        if source_vid not in versions or not _version_active(
                versions[source_vid], date):
            continue
        resolution = resolutions.get(cid)
        chosen_id = resolution["selected_candidate_id"] if resolution else None
        chosen = candidates.get(chosen_id) if chosen_id else None
        target_vid = chosen["target_version_id"] if chosen else None
        target_version = versions.get(target_vid) if target_vid else None
        target_active = bool(target_version and
                             _version_active(target_version, date))

        if target_version and target_version["state"] == "repealed":
            status = "repealed_target"
        elif target_active:
            status = "active"
            nodes.setdefault(
                f"v{target_vid}",
                {"kind": "version", "id": target_vid,
                 "label": target_version["version_label"],
                 "title": target_version["title"],
                 "code": target_version["code"]})
            nodes.setdefault(
                f"u{chosen['target_unit_id']}",
                {"kind": "unit", "id": chosen["target_unit_id"],
                 "version_id": target_vid,
                 "label": chosen["target_desc"]})
        elif resolution and resolution["status"] == "missing":
            status = "missing"
        elif target_version and not _version_active(target_version, date):
            status = "not_in_force"
        else:
            status = resolution["status"] if resolution else "unresolved"

        edges.append({
            "id": cid,
            "source_version_id": source_vid,
            "source_article_num": citation["source_article_num"],
            "signature": citation["signature"],
            "text": citation["text"],
            "sentence": citation["sentence"],
            "target_version_id": target_vid,
            "target_unit_id": target_version and chosen["target_unit_id"],
            "target_desc": chosen["target_desc"] if chosen else None,
            "resolution_status": resolution["status"] if resolution else None,
            "status": status,
            "doc_code": versions[source_vid]["code"],
        })

    problems = [e for e in edges if e["status"] != "active"]
    chains = {e["id"]: _shortest_chain(edges, e["id"]) for e in problems}
    return {"date": date, "nodes": list(nodes.values()),
            "edges": edges, "problems": problems, "chains": chains,
            "counts": {"nodes": len(nodes), "edges": len(edges),
                       "problems": len(problems)}}


def _shortest_chain(edges: list[dict[str, Any]], start_id: int,
                    max_depth: int = 12) -> list[int]:
    """BFS from a broken edge through its target's outgoing edges."""
    by_source: dict[Any, list[dict[str, Any]]] = {}
    for edge in edges:
        by_source.setdefault(edge["source_version_id"], []).append(edge)
    start = next(e for e in edges if e["id"] == start_id)
    queue = deque([(start["target_version_id"], [start_id])])
    seen = {start["source_version_id"]}
    while queue:
        current_vid, path = queue.popleft()
        if len(path) > max_depth:
            return path
        outgoing = by_source.get(current_vid, [])
        if not outgoing:
            return path
        for edge in sorted(outgoing, key=lambda e: e["id"]):
            if edge["status"] != "active" and len(path) > 1:
                return path + [edge["id"]]
            nxt = edge["target_version_id"]
            if nxt and nxt not in seen:
                seen.add(nxt)
                queue.append((nxt, path + [edge["id"]]))
    return [start_id]


def _edge_identity(edge: dict[str, Any]) -> tuple:
    """Stable identity across versions: doc + article + citation signature."""
    return (edge["doc_code"], edge.get("source_article_num"),
            edge["signature"])


def date_diff(self, date_a: str, date_b: str) -> dict[str, Any]:
    graph_a = self.graph_at(date_a)
    graph_b = self.graph_at(date_b)
    key_a = {_edge_identity(e): e for e in graph_a["edges"]}
    key_b = {_edge_identity(e): e for e in graph_b["edges"]}
    added = [e for k, e in key_b.items() if k not in key_a]
    removed = [e for k, e in key_a.items() if k not in key_b]
    redirected = []
    for key in key_a.keys() & key_b.keys():
        old = key_a[key]
        new = key_b[key]
        if (old["target_version_id"], old["target_unit_id"]) != (
                new["target_version_id"], new["target_unit_id"]):
            redirected.append({"from": old, "to": new})
    return {"date_a": date_a, "date_b": date_b,
            "added": added, "removed": removed, "redirected": redirected,
            "counts": {"added": len(added), "removed": len(removed),
                       "redirected": len(redirected)}}
