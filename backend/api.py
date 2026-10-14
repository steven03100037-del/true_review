# -*- coding: utf-8 -*-
"""FastAPI 入口：/api/analyze、/api/settings/cookie、/api/health、静态服务 web/。

报告 JSON 字段名与 docs/V0_开发需求.md 第 3 节逐字一致。
启动：uvicorn backend.api:app --host 0.0.0.0 --port 8000
"""

import importlib.util
import time
import uuid
from pathlib import Path
from typing import Optional

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from backend import cleaner, fetcher, filter_heuristic

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_ROOT / "config_local.py"
WEB_INDEX = PROJECT_ROOT / "web" / "index.html"

app = FastAPI(title="真评", docs_url=None, redoc_url=None)

# ---------------- 请求模型 ----------------


class AnalyzeRequest(BaseModel):
    url: str


class CookieRequest(BaseModel):
    cookie: str


class IngestRequest(BaseModel):
    """Chrome 扩展上报的页面数据（V0.1，见 docs/V0.1_扩展改造笔.md §4）。"""

    product: dict = {}
    page_url: str = ""
    reviews: list = []


# ---------------- 报告内存库（V0.1：扩展入口） ----------------

_REPORTS: dict = {}          # report_id -> {"report": {...}, "page_url": str, "created": float}
_REPORTS_MAX = 50            # 上限 50 份，超出淘汰最旧（玩票足够）


# ---------------- 错误码中文提示 ----------------

ERROR_MESSAGES = {
    "AUTH_REQUIRED": "登录已过期，请在页面下方「设置」中更新淘宝 Cookie",
    "NO_RATE_API": "没有抓到评论数据，商品可能没有评论或页面已改版",
    "TIMEOUT": "抓取超时，请稍后再试",
    "BAD_URL": "链接无法识别，请粘贴淘宝/天猫商品页链接",
}


def _error_report(code: str, message: Optional[str] = None) -> dict:
    """与正常报告同构的错误响应：数据字段置空，error 填结构化错误。"""
    return {
        "product": {"title": None, "image_url": None},
        "grade": None,
        "rating": {"original": None, "adjusted": None},
        "stats": {"total": 0, "genuine": 0, "suspicious": 0, "gray": 0},
        "reviews": [],
        "suspicious": [],
        "meta": {"fetched_pages": 0, "fetched_count": 0, "elapsed_sec": 0},
        "error": {"code": code, "message": message or ERROR_MESSAGES.get(code, "未知错误")},
    }


# ---------------- 报告组装 ----------------

def _tag_sort_key(review: dict) -> tuple:
    """真实评论排序：追评 > 带图 > 细节描述 > 普通。"""
    tags = set(review.get("tags") or [])
    reasons = set(review.get("reasons") or [])
    if "追评" in tags:
        rank = 0
    elif "带图" in tags:
        rank = 1
    elif "细节描述" in reasons:
        rank = 2
    else:
        rank = 3
    return (rank,)


def _public_review(idx: int, review: dict) -> dict:
    return {
        "id": idx,
        "text": review.get("text"),
        "date": review.get("date"),
        "tags": list(review.get("tags") or []),
        "verdict": review.get("verdict"),
    }


def _build_report(product: dict, judged: list, meta: dict, elapsed: float) -> dict:
    summary = filter_heuristic.summarize(judged)
    genuine_gray = [r for r in judged if r.get("verdict") in ("genuine", "gray")]
    genuine_gray.sort(key=_tag_sort_key)
    suspicious = [
        {
            "text": r.get("text"),
            "date": r.get("date"),
            "reasons": list(r.get("reasons") or []),
        }
        for r in judged if r.get("verdict") == "suspicious"
    ]
    return {
        "product": {
            "title": product.get("title"),
            "image_url": product.get("image_url"),
        },
        "grade": summary["grade"],
        "rating": summary["rating"],
        "stats": summary["stats"],
        "reviews": [_public_review(i, r) for i, r in enumerate(genuine_gray, start=1)],
        "suspicious": suspicious,
        "meta": {
            "fetched_pages": meta.get("fetched_pages", 0),
            "fetched_count": meta.get("fetched_count", 0),
            "elapsed_sec": round(elapsed, 1),
            **({"truncated_by_timeout": True} if meta.get("truncated_by_timeout") else {}),
        },
        "error": None,
    }


# ---------------- 路由 ----------------


@app.get("/api/health")
def health():
    return {"ok": True}


@app.post("/api/analyze")
def analyze(req: AnalyzeRequest):
    started = time.monotonic()
    try:
        cookie = fetcher.load_cookie_string()
        fetched = fetcher.fetch_product_reviews(req.url, cookie)
        judged = filter_heuristic.filter_reviews(cleaner.clean_reviews(fetched["reviews"]))
        elapsed = time.monotonic() - started
        return _build_report(fetched["product"], judged, fetched["meta"], elapsed)
    except fetcher.FetchError as exc:
        return _error_report(exc.code, exc.message)
    except Exception as exc:  # 未预期异常：兜底中文错误，不让前端拿到裸 500
        return JSONResponse(
            status_code=500,
            content=_error_report("INTERNAL", f"服务内部错误：{exc}"),
        )


@app.post("/api/ingest")
def ingest(req: IngestRequest):
    """接收扩展上报的评论，复用 V0 过滤管线，产出与 V0 完全同构的报告 JSON。"""
    started = time.monotonic()
    judged = filter_heuristic.filter_reviews(cleaner.clean_reviews(req.reviews))
    meta = {"fetched_pages": 0, "fetched_count": len(req.reviews)}
    report = _build_report(req.product or {}, judged, meta, time.monotonic() - started)

    report_id = "r_" + uuid.uuid4().hex[:8]
    _REPORTS[report_id] = {
        "report": report,
        "page_url": req.page_url,
        "created": time.time(),
    }
    while len(_REPORTS) > _REPORTS_MAX:  # 淘汰最旧
        oldest = min(_REPORTS, key=lambda k: _REPORTS[k]["created"])
        del _REPORTS[oldest]
    return {"report_id": report_id}


@app.get("/api/report/{report_id}")
def get_report(report_id: str):
    entry = _REPORTS.get(report_id)
    if not entry:
        return JSONResponse(
            status_code=404,
            content={"error": {"code": "NOT_FOUND", "message": "报告不存在或已被清理"}},
        )
    return entry["report"]


@app.post("/api/settings/cookie")
def save_cookie(req: CookieRequest):
    """保存淘宝 Cookie 到 config_local.py（保留其它已有配置项）。"""
    old_key = _read_existing_deepseek_key()
    content = (
        '"""本地配置（由「真评」设置页自动写入；此文件不入库）。"""\n\n'
        f"TAOBAO_COOKIE = {req.cookie!r}\n\n"
        f"DEEPSEEK_API_KEY = {old_key!r}\n"
    )
    CONFIG_PATH.write_text(content, encoding="utf-8")
    return {"ok": True}


def _read_existing_deepseek_key() -> str:
    if not CONFIG_PATH.is_file():
        return ""
    try:
        spec = importlib.util.spec_from_file_location("tr_cfg_rw", CONFIG_PATH)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return getattr(module, "DEEPSEEK_API_KEY", "") or ""
    except Exception:
        return ""


@app.get("/")
def index():
    if not WEB_INDEX.is_file():
        return JSONResponse(status_code=404, content={"error": "页面文件缺失"})
    return FileResponse(WEB_INDEX, media_type="text/html")


@app.get("/report.html")
def report_page():
    """扩展报告页（V0.1）：background 打开 /report.html?id=<report_id>。"""
    path = PROJECT_ROOT / "web" / "report.html"
    if not path.is_file():
        return JSONResponse(status_code=404, content={"error": "页面文件缺失"})
    return FileResponse(path, media_type="text/html")
