"""Cross-version contradictions, missing targets and repealed targets."""
from .conftest import ALPHA_2020, ALPHA_2023, BETA_2019


def test_same_number_different_text_is_conflict_until_decided(seeded):
    # 第十五条 is an explicit numeric cite present in 2023; the 2020 text
    # also has an article numbered 15? No -- so import a 2020 version that
    # does contain a differently worded 第十五条 to force the contradiction.
    seeded.import_version(**{
        "code": "ALPHA", "title": "甲法", "label": "甲法-2020b",
        "effective_date": "2020-06-01", "state": "effective",
        "raw_text": (
            "第一章 总则\n"
            "第十四条 过渡条款。\n"
            "第十五条 按照第十五条的规定，旧的主管机关负责解释。\n")})
    rows = seeded.db.query(
        "SELECT c.id, r.status FROM citations c JOIN resolutions r "
        "ON r.citation_id=c.id WHERE c.text='第十五条' ORDER BY c.id")
    statuses = {r["status"] for r in rows}
    # At least one numeric cite sees competing versions and is flagged rather
    # than silently pointing at the newest text.
    assert "cross_version_conflict" in statuses


def test_missing_target_is_reported_not_fabricated(seeded):
    seeded.import_version(**{
        "code": "GAMMA", "title": "丙法", "label": "丙法-2022",
        "effective_date": "2022-01-01", "state": "effective",
        "raw_text": (
            "第一条 本事项适用《不存在的条例》第七条的规定。\n"
            "第二条 依照第九十九条执行。\n")})
    graph = seeded.graph_at("2024-01-01")
    missing = [e for e in graph["edges"] if e["status"] == "missing"]
    assert len(missing) == 2
    chains = seeded.graph_at("2024-01-01")["chains"]
    for edge in missing:
        assert chains[edge["id"]] == [edge["id"]]


def test_repealed_target_edge_is_marked(seeded):
    # Repeal BETA; cross-doc edge must surface as repealed_target.
    beta = next(v for v in seeded.list_versions("BETA"))
    seeded.transition_version(beta["id"], "repealed")
    graph = seeded.graph_at("2024-01-01")
    edge = next(e for e in graph["edges"] if e["text"].startswith("《乙法》"))
    assert edge["status"] == "repealed_target"
    assert graph["chains"][edge["id"]]


def test_candidates_keep_basis_and_sentence(seeded):
    graph = seeded.graph_at("2024-01-01")
    edge = next(e for e in graph["edges"] if e["text"] == "前款")
    detail = seeded.citation_detail(edge["id"])
    assert detail["citation"]["sentence"]
    assert detail["candidates"], "candidates must always be retained"
    assert all("reason" in c["basis"] for c in detail["candidates"])
