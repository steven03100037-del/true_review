# -*- coding: utf-8 -*-
"""V0.2-A llm_layer 测试——monkeypatch chat，零网络。桩一律 finally 恢复。"""

import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend import llm_layer  # noqa: E402


class _Patched:
    """属性打桩 + finally 恢复（防污染后续直调）。"""

    def __init__(self, obj, name, value):
        self.obj, self.name, self.value = obj, name, value

    def __enter__(self):
        self.old = getattr(self.obj, self.name)
        setattr(self.obj, self.name, self.value)
        return self.value

    def __exit__(self, *exc):
        setattr(self.obj, self.name, self.old)
        return False


def _ids_from_prompt(prompt):
    """只解析"评论列表："之后的真实条目 id（prompt 头部的示例输入也有 id，不能算）。"""
    tail = prompt.split("评论列表：")[-1]
    return re.findall(r'"id":\s*"?(\w+)"?', tail)


def _mk_score_chat(fail_ids=()):
    """打分桩：按 prompt 里的 id 生成 {id: {score, dims}}；prompt 命中 fail_ids 则整批返回 None
    （按内容判定而非调用序——BATCH_WORKERS 并发下调用序不确定）。"""
    state = {"n": 0}

    def fake(prompt, system=None):
        state["n"] += 1
        ids = _ids_from_prompt(prompt)
        if fail_ids and any(i in ids for i in fail_ids):
            return None
        out = {}
        for rid in ids:
            out[rid] = {"score": 4, "dims": ["外观时尚"]}
        return out

    fake.state = state
    return fake


def _review(rid, text="一段评价", verdict="genuine", **extra):
    r = {"id": rid, "text": text, "date": "2026-10-01", "tags": [], "verdict": verdict}
    r.update(extra)
    return r


# ---------------- B 打分+维度 ----------------

def test_score_batches_merge_and_count():
    reviews = [_review(i) for i in range(90)]
    fake = _mk_score_chat()
    with _Patched(llm_layer, "chat", fake):
        out = llm_layer.score_and_tag_reviews(reviews)
    assert fake.state["n"] == 3  # 90 / BATCH_SIZE(40) → 3 批
    assert out is not None and len(out) == 90
    assert all(out[str(r["id"])]["score"] == 4 for r in reviews)


def test_score_single_batch_failure_drops_batch():
    reviews = [_review(i) for i in range(90)]
    fake = _mk_score_chat(fail_ids=("40",))  # 第二批（40~79）失败：内容判定，并发下确定
    with _Patched(llm_layer, "chat", fake):
        out = llm_layer.score_and_tag_reviews(reviews)
    assert out is not None
    assert len(out) == 50  # 失败批（40~79 共 40 条）丢弃，其余两批照常（40+10）
    assert "40" not in out and "79" not in out and "0" in out and "80" in out


def test_score_all_fail_returns_none():
    def none_chat(prompt, system=None):
        return None

    with _Patched(llm_layer, "chat", none_chat):
        assert llm_layer.score_and_tag_reviews([_review(1), _review(2)]) is None


# ---------------- C 灰桶从严终审 ----------------

def test_judge_gray_strict_coercion():
    def fake(prompt, system=None):
        ids = _ids_from_prompt(prompt)
        out = {}
        for rid in ids:
            out[rid] = {"verdict": "genuine", "reason": "含具体使用信息"} if rid == "10" else {"verdict": "maybe", "reason": "拿不准"}
        return out

    with _Patched(llm_layer, "chat", fake):
        out = llm_layer.judge_gray_reviews([_review(10), _review(11), _review(12, verdict="gray")])
    assert out["10"] == {"verdict": "genuine", "reason": "含具体使用信息"}
    # 非法/拿不准的判值从严归 suspicious（宁缺毋滥）
    assert out["11"]["verdict"] == "suspicious"
    assert out["12"]["verdict"] == "suspicious"


def test_judge_gray_fail_returns_none():
    def none_chat(prompt, system=None):
        return None

    with _Patched(llm_layer, "chat", none_chat):
        assert llm_layer.judge_gray_reviews([_review(1, verdict="gray")]) is None


# ---------------- A 划重点 ----------------

def test_summarize_structure():
    def fake(prompt, system=None):
        return {"praise": [{"dim": "外观时尚", "note": "多条提到"}],
                "complain": [{"dim": "做工扎实", "note": "一处提到焊点"}],
                "advice": "可放心入手"}

    with _Patched(llm_layer, "chat", fake):
        out = llm_layer.summarize_report([_review(1), _review(2)])
    assert set(out) == {"praise", "complain", "advice"}
    assert out["praise"][0]["dim"] == "外观时尚"


def test_summarize_fail_returns_none():
    def none_chat(prompt, system=None):
        return None

    with _Patched(llm_layer, "chat", none_chat):
        assert llm_layer.summarize_report([_review(1)]) is None


# ---------------- enhance_report 编排 ----------------

class _FakeStore:
    def __init__(self, report):
        self.report = report
        self.puts = 0

    def get(self, rid):
        return self.report

    def put(self, rid, report):
        self.report = report
        self.puts += 1


def _sample_report():
    return {
        "product": {"title": "T", "image_url": None},
        "stats": {"total": 4, "genuine": 2, "suspicious": 1, "gray": 1},
        "reviews": [
            _review(1, "很具体的长期使用评价", verdict="genuine"),
            _review(2, "另一条真实评价", verdict="genuine"),
            _review(3, "拿不准的一条", verdict="gray"),
        ],
        "suspicious": [{"text": "很好", "date": None, "reasons": ["超短模板好评"]}],
        "meta": {"fetched_count": 4, "elapsed_sec": 1.0},
        "llm": {"status": "pending"},
        "error": None,
    }


def test_enhance_done_rebuckets_and_scores():
    def route_chat(prompt, system=None):
        if "灰桶终审" in prompt:
            rid = _ids_from_prompt(prompt)[0]
            return {rid: {"verdict": "genuine", "reason": "含具体使用细节"}}
        if "评论打分" in prompt:
            return {rid: {"score": 5, "dims": ["电池续航很好"]} for rid in _ids_from_prompt(prompt)}
        if "报告划重点" in prompt:
            return {"praise": [{"dim": "电池续航很好", "note": "多条"}],
                    "complain": [], "advice": "无集中吐槽"}
        return None

    report = _sample_report()
    store = _FakeStore(report)
    with _Patched(llm_layer, "chat", route_chat):
        llm_layer.enhance_report("r_x", store.get, store.put)

    assert store.puts == 1
    assert report["llm"]["status"] == "done"
    assert report["llm"]["summary"]["advice"] == "无集中吐槽"
    assert "elapsed_sec" in report["llm"]
    # C 从严终审后 gray 清零、stats 重算
    assert report["stats"] == {"total": 4, "genuine": 3, "suspicious": 1, "gray": 0}
    gray_entry = [r for r in report["reviews"] if r["id"] == 3][0]
    assert gray_entry["verdict"] == "genuine" and gray_entry["llm"] == "含具体使用细节"
    # B 打分写回
    assert report["reviews"][0]["score"] == 5


def test_enhance_gray_to_suspicious_moves_entry():
    def route_chat(prompt, system=None):
        if "灰桶终审" in prompt:
            rid = _ids_from_prompt(prompt)[0]
            return {rid: {"verdict": "suspicious", "reason": "空泛好评，无使用信息"}}
        if "评论打分" in prompt:
            return {rid: {"score": 3, "dims": []} for rid in _ids_from_prompt(prompt)}
        return {"praise": [], "complain": [], "advice": "无集中吐槽"}

    report = _sample_report()
    store = _FakeStore(report)
    with _Patched(llm_layer, "chat", route_chat):
        llm_layer.enhance_report("r_x", store.get, store.put)

    assert report["llm"]["status"] == "done"
    assert report["stats"] == {"total": 4, "genuine": 2, "suspicious": 2, "gray": 0}
    assert all(r["id"] != 3 for r in report["reviews"])
    moved = [s for s in report["suspicious"] if s["text"] == "拿不准的一条"][0]
    assert moved["llm"] == "空泛好评，无使用信息"


def test_enhance_all_fail_marks_failed_keeps_report():
    def none_chat(prompt, system=None):
        return None

    report = _sample_report()
    store = _FakeStore(report)
    with _Patched(llm_layer, "chat", none_chat):
        llm_layer.enhance_report("r_x", store.get, store.put)

    assert report["llm"]["status"] == "failed" and report["llm"]["error"]
    # 报告其余部分原样保留（gray 未动）
    assert report["stats"]["gray"] == 1
    assert len(report["reviews"]) == 3


def test_enhance_missing_report_noop():
    class EmptyStore:
        def get(self, rid):
            return None

        def put(self, rid, report):
            raise AssertionError("不应回写")

    with _Patched(llm_layer, "chat", lambda p, s=None: {}):
        llm_layer.enhance_report("r_missing", EmptyStore().get, EmptyStore().put)


# ---------------- is_enabled ----------------

def test_is_enabled():
    with _Patched(llm_layer, "_api_key", lambda: ""):
        assert llm_layer.is_enabled() is False
    with _Patched(llm_layer, "_api_key", lambda: "sk-demo"):
        assert llm_layer.is_enabled() is True
