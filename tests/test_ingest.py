# -*- coding: utf-8 -*-
"""V0.1 /api/ingest 契约测试——零外部依赖（不碰网、不碰 LLM、不开浏览器）。

扩展 POST 混合评论 → 后端复用过滤管线 → 存内存 → GET 取回报告。
断言方式：报告的 stats/grade/rating 必须与"直接在本进程跑一遍
cleaner+filter_heuristic 管线"的结果完全一致（证明 ingest 复用管线零偏差），
另加 total=50、report_id 格式、404 等锚点。
"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if str(PROJECT_ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "tests"))

from fastapi.testclient import TestClient  # noqa: E402

from backend.api import app  # noqa: E402
from backend import cleaner, filter_heuristic  # noqa: E402
from test_filter import build_mixed  # noqa: E402

client = TestClient(app)


def _expected_summary(reviews):
    judged = filter_heuristic.filter_reviews(cleaner.clean_reviews(reviews))
    return filter_heuristic.summarize(judged)


def test_ingest_creates_report_and_reuses_pipeline():
    reviews = build_mixed()  # 30 种子 + 20 水军，与 V0 管线测试同一数据
    resp = client.post("/api/ingest", json={
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
    assert report["grade"] == expect["grade"]
    assert report["rating"] == expect["rating"]
    assert report["stats"]["total"] == 50
    assert report["error"] is None
    assert report["product"]["title"] == "测试商品标题"
    # suspicious 数组条数与 stats 对齐，且每条带中文理由
    assert len(report["suspicious"]) == expect["stats"]["suspicious"]
    assert all(r["reasons"] for r in report["suspicious"])
    # reviews 数组仅含 genuine + gray（V0 同构）
    assert len(report["reviews"]) == expect["stats"]["genuine"] + expect["stats"]["gray"]


def test_ingest_empty_reviews_returns_u_grade_not_500():
    """扩展超时/零数据同样 POST；零评论 → 空报告 + U 评级，不报 500。"""
    resp = client.post("/api/ingest", json={"product": {}, "page_url": "", "reviews": []})
    assert resp.status_code == 200, resp.text
    report = client.get(f"/api/report/{resp.json()['report_id']}").json()
    assert report["grade"] == "U"
    assert report["stats"] == {"total": 0, "genuine": 0, "suspicious": 0, "gray": 0}
    assert report["reviews"] == [] and report["suspicious"] == []


def test_report_not_found_is_404_json():
    resp = client.get("/api/report/r_nosuch000")
    assert resp.status_code == 404
    body = resp.json()
    assert "error" in body and body["error"]["message"]
