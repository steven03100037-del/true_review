# -*- coding: utf-8 -*-
"""FastAPI 入口：/api/analyze、/api/settings/cookie、/api/health、静态服务 web/。

报告 JSON 字段名与 docs/V0_开发需求.md 第 3 节逐字一致。
启动：uvicorn backend.api:app --host 0.0.0.0 --port 8000
"""

import importlib.util
import time
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
