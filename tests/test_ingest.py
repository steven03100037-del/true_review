# -*- coding: utf-8 -*-
"""V0.1/V0.2-A /api/ingest 契约测试——零外部依赖（不碰网/LLM/真库）。

V0.2-A 变化：报告不再含 grade/rating（自评停算停输出）；顶层 llm 三态
（pending/skipped/failed→done 由后台线程回写）；报告落 SQLite、内存 miss 回源查库。
真实 key 存在时测试必须打桩（is_enabled/enhance_report/db.DB_PATH），绝不真调用。
"""

import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if str(PROJECT_ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from backend import db, llm_layer  # noqa: E402
from backend.api import app  # noqa: E402
from backend import cleaner, filter_heuristic  # noqa: E402
from test_filter import build_mixed  # noqa: E402

client = TestClient(app)


class _Patched:
    """多属性打桩 + with 退出恢复（防污染后续用例）。"""

    def __init__(self, *bindings):
        self.bindings = []
        for obj_name, name, value in bindings:
            obj = globals()[obj_name] if isinstance(obj_name, str) else obj_name
            self.bindings.append((obj, name, getattr(obj, name), value))

    def __enter__(self):
        for obj, name, _, value in self.bindings:
            setattr(obj, name, value)
        return self

    def __exit__(self, *exc):
        for obj, name, old, _ in self.bindings:
            setattr(obj, name, old)
        return False


def _temp_db():
    return Path(tempfile.mkdtemp(prefix="zp_ing_")) / "reports.db"


def _ingest(payload):
    return client.post("/api/ingest", json=payload)


def _expected_summary(reviews):
    judged = filter_heuristic.filter_reviews(cleaner.clean_reviews(reviews))
    return filter_heuristic.summarize(judged)


def test_ingest_creates_report_and_reuses_pipeline():
    reviews = build_mixed()  # 30 种子 + 20 水军，与过滤管线测试同一数据
    with _Patched(
        ("llm_layer", "is_enabled", lambda: True),
        ("llm_layer", "enhance_report", lambda rid, get, put: None),  # 桩：线程体不跑
        ("db", "DB_PATH", _temp_db()),
        ("db", "_init_done", False),
    ):
        resp = _ingest({
            "product": {"title": "测试商品标题", "image_url": "https://img.example/1.jpg"},
            "page_url": "https://detail.tmall.com/item.htm?id=1063884940625",
            "reviews": reviews,
        })
        assert resp.status_code == 200, resp.text
        rid = resp.json()["report_id"]
        assert rid.startswith("r_") and len(rid) == 10  # r_ + uuid4 hex 前 8 位

        got = client.get(f"/api/report/{rid}")
        assert got.status_code == 200
        report = got.json()

        expect = _expected_summary(reviews)
        assert report["stats"] == expect["stats"]
        assert report["stats"]["total"] == 50
        assert report["error"] is None
        assert report["product"]["title"] == "测试商品标题"
        # V0.2-A：grade/rating 停算停输出；顶层 llm=pending（key 已配置路径）
        assert "grade" not in report and "rating" not in report
        assert report["llm"]["status"] == "pending"
        # suspicious 数组条数与 stats 对齐，且每条带中文理由
        assert len(report["suspicious"]) == expect["stats"]["suspicious"]
        assert all(r["reasons"] for r in report["suspicious"])
        # reviews 数组仅含 genuine + gray（V0 同构）
        assert len(report["reviews"]) == expect["stats"]["genuine"] + expect["stats"]["gray"]
        # 落库：内存清掉后 GET 仍可从库回源（服务重启语义）
        assert db.get_report_json(rid) is not None
        assert db.get_report_json(rid)["llm"]["status"] == "pending"


def test_ingest_no_key_skipped_and_no_db():
    reviews = [{"text": "用了三个月质量依旧很好，大小正合适", "date": None, "tags": [], "star": None}]
    db_path = _temp_db()
    with _Patched(
        ("llm_layer", "is_enabled", lambda: False),
        ("db", "DB_PATH", db_path),
        ("db", "_init_done", False),
    ):
        resp = _ingest({"product": {}, "page_url": "", "reviews": reviews})
        assert resp.status_code == 200, resp.text
        rid = resp.json()["report_id"]
        report = client.get(f"/api/report/{rid}").json()
        # 无 key → 全层跳过：规则层形态 + skipped，不建库不跑线程
        assert report["llm"]["status"] == "skipped"
        assert "grade" not in report and "rating" not in report
        assert report["stats"]["total"] == 1
        assert report["reviews"][0]["verdict"] == "genuine"
        assert not db_path.exists()  # 不建库


def test_report_not_found_is_404_json():
    resp = client.get("/api/report/r_nosuch000")
    assert resp.status_code == 404
    body = resp.json()
    assert "error" in body and body["error"]["message"]


def test_report_falls_back_to_db_when_memory_miss():
    """服务重启语义：内存 miss → 查库 data_json。"""
    reviews = [{"text": "很好", "date": None, "tags": [], "star": None},
               {"text": "用了三个月质量依旧很好，大小正合适", "date": None, "tags": [], "star": None}]
    db_path = _temp_db()
    with _Patched(
        ("llm_layer", "is_enabled", lambda: True),
        ("llm_layer", "enhance_report", lambda rid, get, put: None),
        ("db", "DB_PATH", db_path),
        ("db", "_init_done", False),
    ):
        rid = _ingest({"product": {"title": "x"}, "page_url":
                       "https://item.taobao.com/item.htm?id=123456789012",
                       "reviews": reviews}).json()["report_id"]
        # 模拟重启：清内存缓存
        from backend.api import _REPORTS, _REPORTS_LOCK
        with _REPORTS_LOCK:
            _REPORTS.pop(rid, None)
        got = client.get(f"/api/report/{rid}")
        assert got.status_code == 200
        assert got.json()["product"]["title"] == "x"
