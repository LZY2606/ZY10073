import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from citeweave.db import DB  # noqa: E402
from citeweave.service import CiteWeaveService  # noqa: E402

ALPHA_2020 = {
    "code": "ALPHA", "title": "甲法", "label": "甲法-2020",
    "effective_date": "2020-01-01", "adopted_date": "2019-12-01",
    "state": "effective",
    "raw_text": (
        "第一章 总则\n"
        "第一条 为规范行政管理，根据宪法，制定本法。\n"
        "第二条 本法适用于本市行政区域内的活动。\n"
        "第三条 主管部门负责组织实施本法；具体办法依照本章规定执行。\n"
        "第二章 申请与审批\n"
        "第四条 申请人提交材料后，主管部门按照前款要求进行审查。\n"
        "第五条 审批程序参照《乙法》第三条第二款的规定。\n"
        "第六条 本章未尽事宜，适用前条规定。\n"),
}
ALPHA_2023 = {
    "code": "ALPHA", "title": "甲法", "label": "甲法-2023",
    "effective_date": "2023-06-01", "adopted_date": "2023-03-01",
    "state": "effective",
    "raw_text": (
        "第一章 总则\n"
        "第一条 为规范行政管理，保护当事人合法权益，根据宪法，制定本法。\n"
        "第二条 本法适用于本市行政区域内的活动；法律另有规定的除外。\n"
        "第三条 主管部门负责组织实施本法。\n"
        "第二章 申请与审批\n"
        "第四条 申请人提交材料后，主管部门按照前款要求进行审查，并在二十日内决定。\n"
        "第五条 数据共享依照第十五条的规定执行，审批程序参照《乙法》第三条第二款。\n"
        "第六条 本章未尽事宜，适用前条规定。\n"
        "第十五条 市级平台负责数据共享的技术保障工作。\n"),
}
BETA_2019 = {
    "code": "BETA", "title": "乙法", "label": "乙法-2019",
    "effective_date": "2019-05-01", "adopted_date": "2019-01-01",
    "state": "effective",
    "raw_text": (
        "第一章 一般规定\n"
        "第一条 为规范审批行为，制定本法。\n"
        "第二条 审批遵循公开、公平原则。\n"
        "第三条 审批材料应当真实。\n"
        "申请人对材料真实性负责。\n"
        "主管部门可以核查。\n"
        "第四条 违反本法的，依法处理。\n"),
}


@pytest.fixture
def svc(tmp_path):
    db = DB(tmp_path / "citeweave.sqlite3")
    service = CiteWeaveService(db)
    return service


@pytest.fixture
def seeded(svc):
    svc.import_version(**ALPHA_2020)
    svc.import_version(**ALPHA_2023)
    svc.import_version(**BETA_2019)
    return svc
