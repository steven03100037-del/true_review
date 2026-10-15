# -*- coding: utf-8 -*-
"""FastAPI 入口：/api/ingest、/api/report、/api/analyze(兼容保留)、/api/settings/cookie、/api/health、静态服务 web/。

V0.2-A：ingest 走异步 LLM 增值管线——报告先秒开（规则层），后台线程跑
llm_layer.enhance_report（灰桶从严终审/打分+维度/划重点），前端轮询自动刷新；
报告停算停输出 grade/rating；SQLite 三表落库（db.py），内存 miss 回源查库。
启动：uvicorn backend.api:app --host 0.0.0.0 --port 8000
"""

import importlib.util
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from backend import cleaner, fetcher, filter_heuristic
from backend import db, llm_layer

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
    """Chrome 扩展上报的页面数据（V0.1；V0.2-B 起 meta.total_declared 透传）。"""

    product: dict = {}
    page_url: str = ""
    reviews: list = []
    meta: dict = {}


# ---------------- 报告内存库（V0.1：扩展入口；V0.2-A 起与 SQLite 双写） ----------------

_REPORTS: dict = {}          # report_id -> {"report": {...}, "page_url": str, "created": float}
_REPORTS_MAX = 50            # 上限 50 份，超出淘汰最旧（玩票足够）
_REPORTS_LOCK = threading.Lock()


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
        "stats": {"total": 0, "genuine": 0, "suspicious": 0, "gray": 0},
        "reviews": [],
        "suspicious": [],
        "meta": {"fetched_pages": 0, "fetched_count": 0, "elapsed_sec": 0},
        "llm": {"status": "skipped"},
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
        # V0.2-B 采集字段（扩展未升级时缺省为空，不影响展示）
        "images": list(review.get("images") or []),
        "append_text": review.get("append_text"),
        "sku": review.get("sku"),
    }


def _build_report(product: dict, judged: list, meta: dict, elapsed: float) -> dict:
    """规则层报告组装（V0.2-A：grade/rating 停算停输出；judged 注入 _report_no 供落库定位）。"""
    summary = filter_heuristic.summarize(judged)
    genuine_gray = [r for r in judged if r.get("verdict") in ("genuine", "gray")]
    genuine_gray.sort(key=_tag_sort_key)
    for i, r in enumerate(genuine_gray, start=1):
        r["_report_no"] = i  # 落库定位（不进报告输出）
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
        "stats": summary["stats"],
        "reviews": [_public_review(i, r) for i, r in enumerate(genuine_gray, start=1)],
        "suspicious": suspicious,
        "meta": {
            "fetched_pages": meta.get("fetched_pages", 0),
            "fetched_count": meta.get("fetched_count", 0),
            "total_declared": meta.get("total_declared"),
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
    """接收扩展上报的评论：规则层报告秒开；key 配置时落库并起后台 LLM 增值线程。"""
    started = time.monotonic()
    judged = filter_heuristic.filter_reviews(cleaner.clean_reviews(req.reviews))
    meta = {"fetched_pages": 0, "fetched_count": len(req.reviews),
            "total_declared": (req.meta or {}).get("total_declared")}
    report = _build_report(req.product or {}, judged, meta, time.monotonic() - started)

    enabled = llm_layer.is_enabled()
    report["llm"] = {"status": "pending" if enabled else "skipped"}

    report_id = "r_" + uuid.uuid4().hex[:8]
    with _REPORTS_LOCK:
        _REPORTS[report_id] = {
            "report": report,
            "page_url": req.page_url,
            "created": time.time(),
        }
        while len(_REPORTS) > _REPORTS_MAX:  # 淘汰最旧
            oldest = min(_REPORTS, key=lambda k: _REPORTS[k]["created"])
            del _REPORTS[oldest]

    if enabled:
        try:  # 落库失败不阻塞报告返回（内存仍可服务）
            db.save_ingest(report_id, req.page_url, report, judged)
        except Exception:
            pass
        threading.Thread(  # 后台增值；daemon 随主进程退出
            target=llm_layer.enhance_report,
            args=(report_id, _store_get, _store_put),
            daemon=True,
        ).start()
    return {"report_id": report_id}


def _store_get(report_id: str):
    """enhance 取报告：先内存后库（含服务重启后拿到 pending 报告续跑的场景）。"""
    with _REPORTS_LOCK:
        entry = _REPORTS.get(report_id)
        if entry:
            return entry["report"]
    try:
        return db.get_report_json(report_id)
    except Exception:
        return None


def _store_put(report_id: str, report: dict):
    """enhance 回写：内存 + 库（终审数/状态/全文/逐条 verdict-score-dims）。"""
    with _REPORTS_LOCK:
        entry = _REPORTS.get(report_id)
        if entry:
            entry["report"] = report
    try:
        rows = []
        for r in report.get("reviews") or []:
            rows.append({"review_no": r.get("id"), "text": r.get("text"),
                         "verdict": r.get("verdict"), "score": r.get("score"),
                         "dims": r.get("dims") or []})
        for s in report.get("suspicious") or []:
            if "id" in s:  # 原 gray 并入 suspicious 的条目（带 report 内序号）
                rows.append({"review_no": s.get("id"), "text": s.get("text"),
                             "verdict": "suspicious", "score": None, "dims": []})
        db.update_llm_result(report_id, report, rows)
    except Exception:
        pass  # 落库失败不影响内存报告


@app.get("/api/report/{report_id}")
def get_report(report_id: str):
    with _REPORTS_LOCK:
        entry = _REPORTS.get(report_id)
    if entry:
        return entry["report"]
    try:  # 内存 miss（重启/淘汰）→ 查库
        stored = db.get_report_json(report_id)
    except Exception:
        stored = None
    if stored:
        return stored
    return JSONResponse(
        status_code=404,
        content={"error": {"code": "NOT_FOUND", "message": "报告不存在或已被清理"}},
    )


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
