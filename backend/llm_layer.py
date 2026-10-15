# -*- coding: utf-8 -*-
"""LLM 增值层（V0.2-A 全量实现，替代原空壳）。

三件（编排见 enhance_report）：
- C judge_gray_reviews：灰桶从严终审——判 genuine 必须有具体使用信息/真实购买细节；
  模板化好评、空泛夸赞、商家引导话术、信息量不足、拿不准的一律 suspicious（宁缺毋滥）。
- B score_and_tag_reviews：1-5 信息量打分 + DIMS 维度标注，按批并发。
- A summarize_report：genuine 池划重点（在夸/在吐槽/建议）。

纪律：
- 增强层任何失败不影响主报告——chat 失败重试 1 次后返回 None，调用方降级；
- key 为空 → is_enabled()=False，整层跳过；
- DIMS 词表钉死（前端聚合靠它稳定）。

三个 PROMPT 常量即文件头备查文本（PROMPT_JUDGE / PROMPT_SCORE / PROMPT_SUMMARY），
不另复制以免两处漂移。
"""

import json
import re
import time
from concurrent.futures import ThreadPoolExecutor

import importlib.util
from pathlib import Path

import requests

DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"
DEEPSEEK_MODEL = "deepseek-chat"
CHAT_TIMEOUT = 60          # 单请求超时秒
BATCH_SIZE = 40            # 打分批次大小
BATCH_WORKERS = 4          # 打分并发
DIMS = ["电池续航很好", "减震效果出色", "做工扎实", "性价比高",
        "外观时尚", "服务态度很好", "使用体验很棒"]   # 词表钉死，前端聚合靠它稳定

_TEXT_CUT_SCORE = 120      # B 打分入参 text 截断
_TEXT_CUT_SUM = 80         # A 划重点入参 text 截断

_SYSTEM = "你是电商评论分析助手。只输出 JSON，不要输出任何解释文字或 Markdown 代码块标记。"

PROMPT_JUDGE = (
    "【任务：灰桶终审】以下是规则引擎无法判定（gray）的淘宝评论。请逐条判定 verdict：\n"
    '判 "genuine" 必须满足：有具体使用信息或真实购买细节（如使用时长、场景、对比、数据、具体问题）。\n'
    '以下情况一律判 "suspicious"（宁缺毋滥）：模板化好评、空泛夸赞、商家引导话术、'
    "信息量不足、拿不准的。\n"
    '只输出 JSON：{"<id>": {"verdict": "genuine|suspicious", "reason": "<中文短理由>"}}，'
    "每条都要给出。\n"
    '示例输入：[{"id": "7", "text": "收到宝贝很好很喜欢", "date": "2026-10-01", "tags": []}]\n'
    '示例输出：{"7": {"verdict": "suspicious", "reason": "模板化好评，无具体使用信息"}}\n\n'
    "评论列表：\n{items}"
)

PROMPT_SCORE = (
    "【任务：评论打分】对下列真实评论按信息量打分（1-5）并从维度词表中选命中维度：\n"
    "打分标准：5=有具体细节+对比/数据/长期使用（追评）；4=具体但单一；3=有信息但泛；"
    "2=空泛好评；1=无文字。\n"
    '维度词表（只能从中选，可多选也可为空）：' + "、".join(DIMS) + "。\n"
    '只输出 JSON：{"<id>": {"score": <1-5整数>, "dims": ["<维度>", ...]}}，每条都要给出。\n'
    '示例输入：[{"id": "5", "text": "用了三个月，续航和描述一致，做工也不错", "date": "2026-09-01",'
    ' "tags": ["追评"], "append_text": "半年了依旧耐用"}]\n'
    '示例输出：{"5": {"score": 5, "dims": ["电池续航很好", "做工扎实"]}}\n\n'
    "评论列表：\n{items}"
)

PROMPT_SUMMARY = (
    "【任务：报告划重点】以下是某商品的全部真实评论（已过滤水军）。请提炼：\n"
    '1. praise：大家主要在夸什么（≤5 项，每项 {"dim": "<维度或短语>", "note": "<一句话依据>"}）；\n'
    "2. complain：集中吐槽（≤5 项，同结构；无集中吐槽时给空数组）；\n"
    '3. advice：给想买的人的一句话建议（依据以上信息，不编造）。\n'
    '只输出 JSON：{"praise": [...], "complain": [...], "advice": "<一句话>"}。\n'
    '示例输出：{"praise": [{"dim": "电池续航很好", "note": "多条提到续航与标称接近"}],'
    ' "complain": [], "advice": "注重续航的用户可放心入手"}\n\n'
    "真实评论列表：\n{items}"
)

# ---------------- key 与开关 ----------------


def _api_key():
    """读 config_local.DEEPSEEK_API_KEY（importlib 动态加载，与 api.py 同款）；空/缺 → None。"""
    path = Path(__file__).resolve().parent.parent / "config_local.py"
    if not path.is_file():
        return None
    try:
        spec = importlib.util.spec_from_file_location("tr_cfg_llm", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        key = (getattr(module, "DEEPSEEK_API_KEY", "") or "").strip()
        return key or None
    except Exception:
        return None


def is_enabled() -> bool:
    return bool(_api_key())


# ---------------- 基础对话 ----------------


def chat(prompt, system=None):
    """DeepSeek 对话：取 content 并解析 JSON；失败重试 1 次；两次失败返回 None（不抛出）。"""
    key = _api_key()
    if not key:
        return None
    payload = {
        "model": DEEPSEEK_MODEL,
        "messages": ([{"role": "system", "content": system or _SYSTEM}]
                     + [{"role": "user", "content": prompt}]),
        "response_format": {"type": "json_object"},
        "temperature": 0.2,
    }
    for _attempt in (1, 2):
        try:
            resp = requests.post(
                DEEPSEEK_URL, json=payload,
                headers={"Authorization": "Bearer " + key},
                timeout=CHAT_TIMEOUT,
            )
            if resp.status_code != 200:
                continue
            content = resp.json()["choices"][0]["message"]["content"]
            try:
                return json.loads(content)
            except (TypeError, ValueError):
                # 容错：剥掉可能的 ```json 围栏再试一次
                cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", str(content).strip(),
                                 flags=re.MULTILINE)
                try:
                    return json.loads(cleaned)
                except (TypeError, ValueError):
                    continue
        except Exception:
            continue  # 网络/超时/解析失败同路径，不抛出
    return None


# ---------------- 工具 ----------------


def _cut(text, limit):
    t = str(text or "")
    return t[:limit]


def _items_json(reviews, text_limit):
    return json.dumps(
        [{"id": str(r.get("id")), "text": _cut(r.get("text"), text_limit),
          "date": r.get("date"), "tags": r.get("tags") or [],
          "append_text": r.get("append_text")} for r in reviews],
        ensure_ascii=False,
    )


def _fill(template, reviews, text_limit):
    return template.replace("{items}", _items_json(reviews, text_limit))


# ---------------- C 灰桶从严终审 ----------------


def judge_gray_reviews(gray_reviews):
    """入参规则层 gray 条目列表；出参 {id: {"verdict","reason"}}，失败 None。

    非法/缺失判值一律从严归 suspicious（宁缺毋滥）。
    """
    if not gray_reviews:
        return {}
    data = chat(_fill(PROMPT_JUDGE, gray_reviews, 120))
    if not isinstance(data, dict):
        return None
    out = {}
    for r in gray_reviews:
        rid = str(r.get("id"))
        v = data.get(rid) or {}
        verdict = v.get("verdict")
        reason = str(v.get("reason") or "")[:80] or "信息不足，从严判为可疑"
        if verdict != "genuine":  # 只认 genuine，其余一律 suspicious
            verdict = "suspicious"
            if not v.get("reason"):
                reason = "信息不足，从严判为可疑"
        out[rid] = {"verdict": verdict, "reason": reason}
    return out


# ---------------- B 打分+维度 ----------------


def _score_batch(batch):
    data = chat(_fill(PROMPT_SCORE, batch, _TEXT_CUT_SCORE))
    if not isinstance(data, dict):
        return None
    out = {}
    for r in batch:
        rid = str(r.get("id"))
        v = data.get(rid) or {}
        try:
            score = int(v.get("score"))
        except (TypeError, ValueError):
            continue  # 该条无有效打分 → 不写 score
        score = max(1, min(5, score))
        dims = [d for d in (v.get("dims") or []) if d in DIMS]
        out[rid] = {"score": score, "dims": dims}
    return out


def score_and_tag_reviews(reviews):
    """按 BATCH_SIZE 切批并发；单批失败丢该批；全部失败返回 None；出参 {id: {score,dims}}。"""
    if not reviews:
        return {}
    batches = [reviews[i:i + BATCH_SIZE] for i in range(0, len(reviews), BATCH_SIZE)]
    merged = {}
    with ThreadPoolExecutor(max_workers=BATCH_WORKERS) as pool:
        futures = [(b, pool.submit(_score_batch, b)) for b in batches]
        for b, fut in futures:
            try:
                res = fut.result()
            except Exception:
                res = None  # 单批异常丢该批
            if res:
                merged.update(res)
    return merged or None


# ---------------- A 划重点 ----------------


def summarize_report(reviews):
    """入参 genuine 池全量（每条截断 80 字）；出参 {"praise","complain","advice"}，失败 None。"""
    if not reviews:
        return {"praise": [], "complain": [], "advice": "无集中吐槽"}
    data = chat(_fill(PROMPT_SUMMARY, reviews, _TEXT_CUT_SUM))
    if not isinstance(data, dict):
        return None

    def _clip_list(v):
        out = []
        for item in (v or [])[:5]:
            if isinstance(item, dict) and item.get("dim"):
                out.append({"dim": str(item["dim"])[:30],
                            "note": str(item.get("note") or "")[:120]})
        return out

    return {
        "praise": _clip_list(data.get("praise")),
        "complain": _clip_list(data.get("complain")),
        "advice": str(data.get("advice") or "")[:300] or "无集中吐槽",
    }


# ---------------- 编排入口 ----------------


def enhance_report(report_id, store_get, store_put):
    """后台线程调用：C(gray 非空才跑) → B(genuine 池) → A(genuine 池) → store_put 回写。

    全程 try/except：任何异常 → llm = {"status": "failed", "error": 摘要}，报告其余部分原样。
    """
    report = None
    try:
        report = store_get(report_id)
        if not report:
            return
        started = time.time()
        reviews = report.get("reviews") or []
        suspicious = report.get("suspicious") or []
        gray = [r for r in reviews if r.get("verdict") == "gray"]
        genuine = [r for r in reviews if r.get("verdict") == "genuine"]
        any_success = False

        # C：灰桶从严终审（gray 非空才跑）
        if gray:
            verdicts = judge_gray_reviews(gray)
            if verdicts is not None:
                any_success = True
                for r in gray:
                    v = verdicts.get(str(r.get("id"))) or {}
                    if v.get("verdict") == "genuine":
                        r["verdict"] = "genuine"
                        r["llm"] = v.get("reason")
                        genuine.append(r)
                    else:
                        r["verdict"] = "suspicious"
                        suspicious.append({
                            "id": r.get("id"),  # 供 db 回写定位（前端忽略）
                            "text": r.get("text"), "date": r.get("date"),
                            "reasons": ["AI 终审：信息量不足或疑似模板"],
                            "llm": v.get("reason"),
                        })
                report["reviews"] = genuine  # gray 全部离场（原列表仅含 genuine+gray）

        # stats 重算：基于终态列表（C 成功则 gray 自然清零；C 失败如实保留 gray 计数）
        cur = report.get("reviews") or []
        n_gen = sum(1 for r in cur if r.get("verdict") == "genuine")
        n_gray = sum(1 for r in cur if r.get("verdict") == "gray")
        report["stats"] = {"total": n_gen + len(suspicious) + n_gray,
                           "genuine": n_gen, "suspicious": len(suspicious), "gray": n_gray}

        # B：genuine 池打分+维度（取终态列表）
        genuine_pool = [r for r in cur if r.get("verdict") == "genuine"]
        scored = score_and_tag_reviews(genuine_pool)
        if scored:
            any_success = True
            for r in genuine_pool:
                s = scored.get(str(r.get("id")))
                if s:
                    r["score"] = s["score"]
                    r["dims"] = s.get("dims") or []

        # A：genuine 池划重点
        summary = summarize_report(genuine_pool)
        if summary:
            any_success = True

        if not any_success:
            raise RuntimeError("LLM 三件全部失败（网络或接口异常）")

        report["llm"] = {"status": "done", "summary": summary,
                         "elapsed_sec": round(time.time() - started)}
        store_put(report_id, report)
    except Exception as exc:
        try:
            if report is None:
                report = store_get(report_id)
            if report is not None:
                report["llm"] = {"status": "failed", "error": str(exc)[:200]}
                store_put(report_id, report)
        except Exception:
            pass
