# -*- coding: utf-8 -*-
"""SQLite 三表落库（V0.2-A）：goods 商品档案 / reports 采集批次 / reviews 逐条评论。

设计见 docs/V0.2-A_LLM增值与报告改版笔.md §3：
- 报告全文（data_json）与结构化行两份都存：全文保真、行表供查询；
- ingest 时写 goods+reports（llm_status=pending）+reviews（规则层结果）；
  LLM enhance 完成时 update_llm_result 回写终审与打分；
- api.py 不直接写 SQL，全部走本文件函数式接口；
- backend/data/ 已 .gitignore，不入库。
"""

import hashlib
import json
import re
import sqlite3
import time
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent / "data" / "reports.db"
_init_done = False

_ID_RE = re.compile(r"[?&]id=(\d{6,})")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS goods (
  goods_id   TEXT PRIMARY KEY,
  platform   TEXT,
  item_id    TEXT, title TEXT, image_url TEXT,
  first_seen REAL, last_seen REAL);
CREATE TABLE IF NOT EXISTS reports (
  id TEXT PRIMARY KEY, goods_id TEXT, created REAL,
  total_declared INTEGER, fetched_count INTEGER,
  genuine INTEGER, suspicious INTEGER,
  llm_status TEXT, elapsed_sec REAL,
  data_json TEXT);
CREATE INDEX IF NOT EXISTS idx_reports_goods ON reports(goods_id, created DESC);
CREATE TABLE IF NOT EXISTS reviews (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  report_id TEXT, goods_id TEXT, review_no INTEGER,
  text TEXT, append_text TEXT, date TEXT, sku TEXT,
  images_json TEXT, tags_json TEXT,
  verdict TEXT, score INTEGER, dims_json TEXT,
  dup_key TEXT);
CREATE INDEX IF NOT EXISTS idx_reviews_goods ON reviews(goods_id);
"""


def _ensure():
    """建目录建表（进程内只跑一次；测试换库后由用例重置 _init_done）。"""
    global _init_done
    if _init_done:
        return
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    try:
        con.executescript(_SCHEMA)
        con.commit()
    finally:
        con.close()
    _init_done = True


def _connect():
    _ensure()
    return sqlite3.connect(DB_PATH)


def goods_id_from_url(page_url: str):
    """从 page_url 解析 (goods_id, platform, item_id)。

    商品 id 解析失败时 goods_id 用整串 URL 哈希兜底（同 URL 稳定同 id）。
    """
    url = page_url or ""
    platform = None
    if "tmall.com" in url:
        platform = "tmall"
    elif "taobao.com" in url:
        platform = "taobao"
    m = _ID_RE.search(url)
    item_id = m.group(1) if m else None
    key = (platform or "") + "|" + (item_id or url)
    goods_id = hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]
    return goods_id, platform, item_id


def save_ingest(report_id: str, page_url: str, report: dict, judged: list):
    """ingest 落库：goods upsert + reports（llm_status=pending）+ reviews（规则层结果）。

    judged：api 侧过滤管线的内部条目（genuine/gray 带 _report_no，suspicious 不带）。
    """
    goods_id, platform, item_id = goods_id_from_url(page_url)
    product = report.get("product") or {}
    stats = report.get("stats") or {}
    meta = report.get("meta") or {}
    llm = report.get("llm") or {}
    now = time.time()

    con = _connect()
    try:
        con.execute(
            "INSERT INTO goods (goods_id, platform, item_id, title, image_url, first_seen, last_seen)"
            " VALUES (?,?,?,?,?,?,?)"
            " ON CONFLICT(goods_id) DO UPDATE SET last_seen=excluded.last_seen,"
            " title=excluded.title, image_url=excluded.image_url",
            (goods_id, platform, item_id, product.get("title"), product.get("image_url"),
             now, now),
        )
        con.execute(
            "INSERT OR REPLACE INTO reports (id, goods_id, created, total_declared,"
            " fetched_count, genuine, suspicious, llm_status, elapsed_sec, data_json)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (report_id, goods_id, now,
             meta.get("total_declared"), meta.get("fetched_count"),
             stats.get("genuine"), stats.get("suspicious"),
             llm.get("status", "pending"), meta.get("elapsed_sec"),
             json.dumps(report, ensure_ascii=False)),
        )
        for r in judged or []:
            con.execute(
                "INSERT INTO reviews (report_id, goods_id, review_no, text, append_text,"
                " date, sku, images_json, tags_json, verdict, score, dims_json, dup_key)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (report_id, goods_id, r.get("_report_no"), r.get("text"),
                 r.get("append_text"), r.get("date"), r.get("sku"),
                 json.dumps(r.get("images") or [], ensure_ascii=False),
                 json.dumps(r.get("tags") or [], ensure_ascii=False),
                 r.get("verdict"), None, None, r.get("_dup_key")),
            )
        con.commit()
    finally:
        con.close()


def update_llm_result(report_id: str, report: dict, review_rows: list):
    """LLM enhance 完成回写：reports 终审字段+全文；reviews 的 verdict/score/dims。

    review_rows：api 侧组装的终局判定（genuine 池 + 含并入 suspicious 的原 gray），
    每条 {review_no, text, verdict, score, dims}——review_no 命中优先，缺失按 text 兜底。
    """
    stats = report.get("stats") or {}
    llm = report.get("llm") or {}
    meta = report.get("meta") or {}
    con = _connect()
    try:
        con.execute(
            "UPDATE reports SET genuine=?, suspicious=?, llm_status=?, data_json=? WHERE id=?",
            (stats.get("genuine"), stats.get("suspicious"),
             llm.get("status", "done"),
             json.dumps(report, ensure_ascii=False), report_id),
        )
        for row in review_rows or []:
            score = row.get("score")
            con.execute(
                "UPDATE reviews SET verdict=?, score=?, dims_json=?"
                " WHERE report_id=? AND ((? IS NOT NULL AND review_no=?) OR (review_no IS NULL AND text=?))",
                (row.get("verdict"), score,
                 json.dumps(row.get("dims") or [], ensure_ascii=False),
                 report_id, row.get("review_no"), row.get("review_no"), row.get("text")),
            )
        con.commit()
    finally:
        con.close()


def get_report_json(report_id: str):
    """取报告全文（报告页渲染以它为准）；不存在返回 None。"""
    con = _connect()
    try:
        row = con.execute("SELECT data_json FROM reports WHERE id=?", (report_id,)).fetchone()
    finally:
        con.close()
    if not row:
        return None
    try:
        return json.loads(row[0])
    except (TypeError, ValueError):
        return None


def list_reports_by_goods(goods_id: str):
    """按商品列采集批次（V2"我的档案"预留，本期不接前端）。"""
    con = _connect()
    try:
        rows = con.execute(
            "SELECT id, created, llm_status, genuine, suspicious, total_declared, fetched_count"
            " FROM reports WHERE goods_id=? ORDER BY created DESC",
            (goods_id,),
        ).fetchall()
    finally:
        con.close()
    keys = ("id", "created", "llm_status", "genuine", "suspicious",
            "total_declared", "fetched_count")
    return [dict(zip(keys, r)) for r in rows]
