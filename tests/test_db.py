# -*- coding: utf-8 -*-
"""V0.2-A db.py 三表落库测试——零外部依赖（临时 SQLite 文件，不碰网/LLM）。"""

import json
import sqlite3
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend import db  # noqa: E402


def _fresh_db():
    """每个用例独立临时库。"""
    tmpdir = tempfile.mkdtemp(prefix="zp_db_")
    db.DB_PATH = Path(tmpdir) / "reports.db"
    db._init_done = False
    return db


def _sample_report(llm_status="pending"):
    return {
        "product": {"title": "测试商品", "image_url": "https://img.example/1.jpg"},
        "stats": {"total": 3, "genuine": 2, "suspicious": 1, "gray": 0},
        "reviews": [
            {"id": 1, "text": "很好用的商品推荐", "date": "2026-10-01", "tags": ["带图"],
             "verdict": "genuine", "images": ["https://img.example/a.jpg"], "append_text": None, "sku": "标准版"},
            {"id": 2, "text": "用了三个月质量依旧很好", "date": "2026-10-02", "tags": [],
             "verdict": "genuine", "images": [], "append_text": "追加：依然好用", "sku": None},
        ],
        "suspicious": [
            {"text": "很好", "date": "2026-10-03", "reasons": ["超短模板好评"]},
        ],
        "meta": {"fetched_pages": 0, "fetched_count": 3, "total_declared": 1024, "elapsed_sec": 1.2},
        "llm": {"status": llm_status},
        "error": None,
    }


def _sample_judged():
    """内部评论条目（api ingest 传给 db 的形态）：genuine/gray 带 _report_no，suspicious 不带。"""
    return [
        {"text": "很好用的商品推荐", "date": "2026-10-01", "tags": ["带图"], "star": None,
         "verdict": "genuine", "_report_no": 1, "_dup_key": "很好用的商品推荐",
         "images": ["https://img.example/a.jpg"], "append_text": None, "sku": "标准版"},
        {"text": "用了三个月质量依旧很好", "date": "2026-10-02", "tags": [], "star": None,
         "verdict": "genuine", "_report_no": 2, "_dup_key": "用了三个月质量依旧很好",
         "images": [], "append_text": "追加：依然好用", "sku": None},
        {"text": "很好", "date": "2026-10-03", "tags": [], "star": None,
         "verdict": "suspicious", "_report_no": None, "_dup_key": "很好",
         "images": [], "append_text": None, "sku": None},
    ]


def test_save_then_update_then_get_roundtrip():
    db = _fresh_db()
    report = _sample_report()
    db.save_ingest("r_aaa11111", "https://detail.tmall.com/item.htm?id=1063884940625",
                   report, _sample_judged())

    # ingest 后：llm pending 原样存档，可取回
    got = db.get_report_json("r_aaa11111")
    assert got is not None and got["llm"]["status"] == "pending"
    assert got["meta"]["total_declared"] == 1024

    # 三表行数：goods 1 / reports 1 / reviews 3
    con = sqlite3.connect(db.DB_PATH)
    g = con.execute("SELECT COUNT(*) FROM goods").fetchone()[0]
    r = con.execute("SELECT COUNT(*) FROM reports").fetchone()[0]
    v = con.execute("SELECT COUNT(*) FROM reviews").fetchone()[0]
    con.close()
    assert (g, r, v) == (1, 1, 3), (g, r, v)

    # LLM 终审回写：gray 一条并入 suspicious 的终局报告
    final = _sample_report(llm_status="done")
    final["llm"] = {"status": "done",
                    "summary": {"praise": [{"dim": "外观时尚", "note": "多条提到"}],
                                "complain": [], "advice": "无集中吐槽"},
                    "elapsed_sec": 58}
    final["stats"] = {"total": 3, "genuine": 2, "suspicious": 1, "gray": 0}
    final["reviews"][1]["score"] = 4
    final["reviews"][1]["dims"] = ["做工扎实"]
    rows = [
        {"review_no": 1, "text": "很好用的商品推荐", "verdict": "genuine", "score": 5, "dims": ["外观时尚"]},
        {"review_no": 2, "text": "用了三个月质量依旧很好", "verdict": "genuine", "score": 4, "dims": ["做工扎实"]},
        {"review_no": None, "text": "很好", "verdict": "suspicious", "score": None, "dims": []},
    ]
    db.update_llm_result("r_aaa11111", final, rows)

    got = db.get_report_json("r_aaa11111")
    assert got["llm"]["status"] == "done" and got["llm"]["elapsed_sec"] == 58
    assert got["reviews"][1]["score"] == 4

    # reviews 表终审回写：verdict/score/dims
    con = sqlite3.connect(db.DB_PATH)
    rows_db = con.execute(
        "SELECT review_no, verdict, score, dims_json FROM reviews ORDER BY review_no").fetchall()
    con.close()
    by_no = {rn: (vd, sc, dj) for rn, vd, sc, dj in rows_db}
    assert by_no[1] == ("genuine", 5, json.dumps(["外观时尚"], ensure_ascii=False))
    assert by_no[2] == ("genuine", 4, json.dumps(["做工扎实"], ensure_ascii=False))
    assert by_no[None] == ("suspicious", None, "[]")

    # reports 表终审字段
    con = sqlite3.connect(db.DB_PATH)
    llm_status, genu, susp, dj = con.execute(
        "SELECT llm_status, genuine, suspicious, data_json FROM reports WHERE id=?",
        ("r_aaa11111",)).fetchone()
    con.close()
    assert llm_status == "done" and (genu, susp) == (2, 1)
    assert json.loads(dj)["llm"]["status"] == "done"


def test_goods_aggregation_same_item_two_reports():
    db = _fresh_db()
    url = "https://detail.tmall.com/item.htm?id=1063884940625"
    db.save_ingest("r_aaa11111", url, _sample_report(), _sample_judged())
    db.save_ingest("r_bbb22222", url, _sample_report(), _sample_judged())

    con = sqlite3.connect(db.DB_PATH)
    g = con.execute("SELECT COUNT(*), platform, item_id FROM goods").fetchone()
    r = con.execute("SELECT COUNT(*) FROM reports").fetchone()[0]
    con.close()
    assert g[0] == 1 and g[1] == "tmall" and g[2] == "1063884940625"
    assert r == 2

    rows = db.list_reports_by_goods(db.goods_id_from_url(url)[0])
    assert [x["id"] for x in rows] == ["r_bbb22222", "r_aaa11111"]  # created DESC


def test_goods_id_parse_fallback():
    # 无法解析 item id → 整串 URL 哈希，同 URL 稳定同 id
    gid1, plat, iid = db.goods_id_from_url("https://example.com/weird-page")
    assert iid is None and plat is None
    assert gid1 == db.goods_id_from_url("https://example.com/weird-page")[0]
    assert len(gid1) == 16


def test_get_missing_returns_none():
    db = _fresh_db()
    assert db.get_report_json("r_nope0000") is None
