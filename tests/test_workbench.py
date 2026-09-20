from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from contextlib import closing
from pathlib import Path

from citeweave.parser import PARSER_VERSION

from conftest import request_json


V2020 = """# 第一章 总则
第一条 为了规范活动，制定本法。
第二条 基本规则适用第一条。
第三条 特别程序适用第二条。
第四条 本章另有规定的除外。
第五条 补充事项适用前款。
第六条
第一款 第一项规则如下。
第二款 前款规则需要记录。
"""

V2021 = """# 第一章 总则
第一条 为了规范活动，制定本法，并明确新原则。
第二条 基本规则已经调整。
第三条 特别程序适用第二条。
第四条 本章另有规定的除外。
第五条 补充事项适用前款。
第六条
第一款 第一项规则如下。
第二款 前款规则需要记录。
"""


def import_version(server, label="2020-01-01", text=V2020, status="active", date="2020-01-01"):
    return request_json(
        server,
        "/api/documents",
        "POST",
        {
            "doc_key": "law",
            "version_label": label,
            "title": "测试法",
            "raw_text": text,
            "effective_date": date,
            "repealed_date": None,
            "status": status,
        },
    )


def get_doc(server, document_id):
    return request_json(server, f"/api/documents/{document_id}")[1]


def test_import_preserves_raw_text_and_resolves_candidates(server):
    status, result = import_version(server)
    assert status == 201
    detail = get_doc(server, result["document"]["id"])
    assert detail["raw_text"] == V2020
    assert detail["normalized_text"] != detail["raw_text"]
    assert detail["parser_version"] == PARSER_VERSION
    explicit = next(item for item in detail["citations"] if item["quote"] == "第一条")
    assert explicit["sentence"] == "基本规则适用第一条。"
    assert explicit["candidates"]
    assert explicit["resolution_reason"]
    assert explicit["selected_candidate_id"]
    positional = next(item for item in detail["citations"] if item["quote"] == "前款")
    target = next(candidate for candidate in positional["candidates"] if candidate["exact"])
    assert target["target_locator"].endswith("第六条#第1款")


def test_import_preserves_exact_crlf_original(server):
    raw_text = "# 第一章 总则\r\n第一条 原文换行必须保留。\r\n第二条 适用第一条。\r\n"
    status, imported = request_json(
        server,
        "/api/documents",
        "POST",
        {
            "doc_key": "crlf",
            "version_label": "2020",
            "title": "CRLF",
            "raw_text": raw_text,
            "effective_date": "2020-01-01",
            "status": "active",
        },
    )
    assert status == 201
    detail = get_doc(server, imported["document"]["id"])
    assert detail["raw_text"] == raw_text
    assert "\r" not in detail["normalized_text"]


def test_duplicate_request_is_idempotent(server):
    first_status, first = import_version(server)
    second_status, second = import_version(server)
    assert first_status == 201
    assert second_status == 200
    assert second["duplicate"] is True
    assert first["document"]["id"] == second["document"]["id"]
    status, documents = request_json(server, "/api/documents")
    assert len(documents["documents"]) == 1


def test_explicit_idempotency_key_replays_response(server):
    body = {
        "doc_key": "idem",
        "version_label": "2020-01-01",
        "title": "幂等法",
        "raw_text": "第一条 稳定文本。",
        "effective_date": "2020-01-01",
        "status": "ready",
    }
    headers = {"Idempotency-Key": "import-idem-1"}
    first_status, first = request_json(server, "/api/documents", "POST", body, headers)
    second_status, second = request_json(server, "/api/documents", "POST", body, headers)
    assert first_status == second_status == 201
    assert first["document"]["id"] == second["document"]["id"]


def test_illegal_status_transition_returns_409_and_keeps_state(server):
    _, imported = import_version(server)
    document_id = imported["document"]["id"]
    status, payload = request_json(
        server,
        f"/api/documents/{document_id}/transition",
        "POST",
        {"status": "repealed", "expected_version": 1},
    )
    assert status == 409
    assert payload["code"] == "illegal_transition"
    assert get_doc(server, document_id)["status"] == "active"


def test_superseded_document_can_be_repealed(server):
    _, imported = import_version(server)
    document_id = imported["document"]["id"]
    status, _ = request_json(
        server,
        f"/api/documents/{document_id}/transition",
        "POST",
        {"status": "superseded", "expected_version": 1},
    )
    assert status == 200
    status, repealed = request_json(
        server,
        f"/api/documents/{document_id}/transition",
        "POST",
        {"status": "repealed", "expected_version": 2},
    )
    assert status == 200
    assert repealed["status"] == "repealed"


def test_optimistic_conflict_returns_both_differences(server):
    import_version(server)
    _, citations = request_json(server, "/api/citations")
    citation = next(item for item in citations["citations"] if item["quote"] == "第二条")
    article_two = next(
        candidate["target_clause_id"] for candidate in citation["candidates"] if candidate["ordinal"] == 1
    )
    detail = get_doc(server, citation["document_id"])
    article_three = next(
        clause["id"] for clause in detail["clauses"] if clause["level"] == "article" and clause["number_value"] == 3
    )
    status, first = request_json(
        server,
        f"/api/citations/{citation['id']}/corrections",
        "POST",
        {
            "researcher": "alice",
            "target_clause_id": article_two,
            "expected_version": citation["lock_version"],
            "rationale": "版本一",
        },
    )
    assert status == 201
    status, conflict = request_json(
        server,
        f"/api/citations/{citation['id']}/corrections",
        "POST",
        {
            "researcher": "bob",
            "target_clause_id": article_three,
            "expected_version": 1,
            "rationale": "落后写入",
        },
    )
    assert status == 409
    difference = conflict["difference"]
    if "existing_context" in difference:
        assert difference["existing_context"][0]["researcher"] == "alice"
    else:
        assert difference["current"]["lock_version"] > 1
        assert any(decision["researcher"] == "alice" for decision in difference["current"]["decisions"])
    assert difference["client_request"]["researcher"] == "bob"


def test_same_researcher_can_replace_own_target(server):
    import_version(server)
    _, citations = request_json(server, "/api/citations")
    citation = next(item for item in citations["citations"] if item["quote"] == "第二条")
    detail = get_doc(server, citation["document_id"])
    article_two = next(clause["id"] for clause in detail["clauses"] if clause["semantic_key"] == "law|第一章|第二条")
    article_three = next(clause["id"] for clause in detail["clauses"] if clause["semantic_key"] == "law|第一章|第三条")
    for target in (article_two, article_three):
        status, payload = request_json(
            server,
            f"/api/citations/{citation['id']}/corrections",
            "POST",
            {
                "researcher": "alice",
                "target_clause_id": target,
                "expected_version": payload["lock_version"] if 'payload' in locals() else citation["lock_version"],
            },
        )
        assert status == 201
    active = [decision for decision in payload["decisions"] if decision["status"] == "active"]
    assert len(active) == 1
    assert active[0]["target_clause_id"] == article_three


def test_merge_preserves_both_researcher_contexts_without_overwrite(server):
    import_version(server)
    _, citations = request_json(server, "/api/citations")
    citation = next(item for item in citations["citations"] if item["quote"] == "第二条")
    _, documents = request_json(server, "/api/documents")
    detail = get_doc(server, documents["documents"][0]["id"])
    first_target = citation["candidates"][0]["target_clause_id"]
    second_target = next(
        clause["id"] for clause in detail["clauses"] if clause["level"] == "article" and clause["number_value"] == 3
    )
    status, merged = request_json(
        server,
        f"/api/citations/{citation['id']}/merge",
        "POST",
        {
            "researcher": "coordinator",
            "first": {
                "researcher": "alice",
                "target_clause_id": first_target,
                "rationale": "alice page 12",
            },
            "second": {
                "researcher": "bob",
                "target_clause_id": second_target,
                "rationale": "bob page 18",
            },
        },
    )
    assert status == 200
    assert merged["resolution_status"] == "divergent"
    researchers = {decision["researcher"] for decision in merged["decisions"] if decision["status"] == "active"}
    assert researchers == {"alice", "bob"}


def test_time_graph_compare_and_historical_version_not_replaced(server):
    _, first = import_version(server)
    first_id = first["document"]["id"]
    request_json(
        server,
        f"/api/documents/{first_id}/transition",
        "POST",
        {"status": "superseded", "expected_version": 1},
    )
    _, second_import = import_version(server, label="2021-01-01", text=V2021, date="2021-01-01")
    graph_2020 = request_json(server, "/api/graph?date=2020-06-01")[1]
    historical_edges = [
        edge for edge in graph_2020["edges"] if edge["source_version_label"] == "2020-01-01"
    ]
    assert historical_edges
    assert all(edge["target_version_label"] == "2020-01-01" for edge in historical_edges if edge["target_version_label"])
    comparison = request_json(server, "/api/compare?left=2020-06-01&right=2021-06-01")[1]
    assert comparison["added"]
    assert comparison["disappeared"]


def test_published_snapshot_remains_immutable_and_export_is_deterministic(server):
    import_version(server)
    first_export = request_json(server, "/api/export")[1]
    status, snapshot = request_json(
        server,
        "/api/snapshots",
        "POST",
        {"label": "s1", "at_date": "2020-01-01", "created_by": "alice"},
    )
    assert status == 201
    _, citations = request_json(server, "/api/citations")
    citation = citations["citations"][0]
    target = citation["candidates"][-1]["target_clause_id"]
    request_json(
        server,
        f"/api/citations/{citation['id']}/corrections",
        "POST",
        {"researcher": "alice", "target_clause_id": target, "expected_version": citation["lock_version"]},
    )
    _, stored_snapshot = request_json(server, "/api/snapshots/s1")
    assert stored_snapshot["payload"]["package"]["package_fingerprint"] == first_export["package_fingerprint"]
    assert stored_snapshot["package_fingerprint"] == first_export["package_fingerprint"]
    second_export = request_json(server, "/api/export")[1]
    assert json.dumps(first_export, sort_keys=True, ensure_ascii=False) != json.dumps(
        second_export, sort_keys=True, ensure_ascii=False
    )
    assert second_export["decisions"]


def test_anchor_wrapping_detection(server):
    _, imported = import_version(server)
    wrapped = V2020.replace("\n", "  \n")
    status, verification = request_json(
        server,
        "/api/anchors/verify",
        "POST",
        {"document_id": imported["document"]["id"], "raw_text": wrapped},
    )
    assert status == 200
    assert verification["status"] == "wrapping_changed"
    assert verification["anchors_still_apply_by_locator"] is True


def test_recovery_after_process_kill(tmp_path):
    with closing(socket.socket()) as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    db_path = tmp_path / "recovery.db"
    env = dict(os.environ)
    process = subprocess.Popen(
        [sys.executable, "-m", "citeweave", "--host", "127.0.0.1", "--port", str(port), "--db", str(db_path)],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.time() + 5
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=0.2):
                    break
            except OSError:
                time.sleep(0.05)
        body = json.dumps(
            {
                "doc_key": "law",
                "version_label": "2020-01-01",
                "title": "恢复法",
                "raw_text": V2020,
                "effective_date": "2020-01-01",
                "status": "active",
            }
        ).encode()
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/documents",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=2):
            pass
        os.kill(process.pid, signal.SIGKILL)
        process.wait(timeout=5)
        recovered = subprocess.Popen(
            [sys.executable, "-m", "citeweave", "--host", "127.0.0.1", "--port", str(port), "--db", str(db_path)],
            cwd=Path(__file__).resolve().parents[1],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            deadline = time.time() + 5
            while time.time() < deadline:
                try:
                    with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/documents", timeout=0.2) as response:
                        payload = json.loads(response.read())
                    assert len(payload["documents"]) == 1
                    assert payload["documents"][0]["doc_key"] == "law"
                    break
                except (OSError, AssertionError):
                    time.sleep(0.05)
            else:
                raise AssertionError("database did not recover after forced process termination")
        finally:
            recovered.terminate()
            recovered.wait(timeout=5)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
