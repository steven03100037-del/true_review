# -*- coding: utf-8 -*-
"""过滤管线单元测试——零外部依赖（不碰网、不碰 LLM、不开浏览器）。

先写测试后写实现（TDD）。断言目标见 docs/V0_开发需求.md 第 5 节：
- 手编水军样本全部命中 suspicious 且理由正确；
- 真实种子不误杀（gray 可接受）；
- 评级映射边界值。
"""

import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend import cleaner, filter_heuristic  # noqa: E402

FIXTURE = json.loads(
    (PROJECT_ROOT / "tests" / "fixtures" / "comments_fake.json").read_text(encoding="utf-8")
)


def build_mixed():
    """seeds 与水军混排（确定性交错，模拟真实水军密度），保留 _case 身份。"""
    seeds = [dict(x, _case="seed") for x in FIXTURE["seeds"]]
    spam = (
        [dict(x, _case="short") for x in FIXTURE["spam"]["short"]]
        + [dict(x, _case="cluster") for x in FIXTURE["spam"]["cluster"]]
        + [dict(x, _case="template") for x in FIXTURE["spam"]["template"]]
    )
    mixed, si, pi, turn = [], 0, 0, 0
    while si < len(seeds) or pi < len(spam):
        if turn % 3 == 2 and pi < len(spam):
            mixed.append(spam[pi])
            pi += 1
        elif si < len(seeds):
            mixed.append(seeds[si])
            si += 1
        else:
            mixed.append(spam[pi])
            pi += 1
        turn += 1
    return mixed


def run_pipeline(rows):
    cleaned = cleaner.clean_reviews(rows)
    return filter_heuristic.filter_reviews(cleaned)


def judged_by_case():
    judged = run_pipeline(build_mixed())
    groups = {}
    for r in judged:
        groups.setdefault(r["_case"], []).append(r)
    return judged, groups


# ---------- 水军样本：全部落网且理由正确 ----------

def test_spam_all_caught():
    _, groups = judged_by_case()
    for case in ("short", "cluster", "template"):
        assert groups.get(case), f"fixture 缺少 {case} 样本"
        for r in groups[case]:
            assert r["verdict"] == "suspicious", (case, r["text"], r["reasons"])


def test_short_reasons():
    _, groups = judged_by_case()
    for r in groups["short"]:
        assert "超短模板好评" in r["reasons"], (r["text"], r["reasons"])
    dups = [r for r in groups["short"] if r["text"] == "很好"]
    assert len(dups) >= 2, "fixture 应包含重复的超短好评"
    assert any("重复评论" in r["reasons"] for r in dups), "重复条目应带 重复评论 理由"


def test_cluster_reasons():
    _, groups = judged_by_case()
    for r in groups["cluster"]:
        assert any(x.startswith("与同簇") for x in r["reasons"]), (r["text"], r["reasons"])
        assert any(x.endswith("条高度相似") for x in r["reasons"])


def test_template_reasons():
    _, groups = judged_by_case()
    for r in groups["template"]:
        assert "模板化刷单" in r["reasons"], (r["text"], r["reasons"])


# ---------- 真实种子：不误杀 ----------

def test_seeds_not_killed():
    _, groups = judged_by_case()
    assert len(groups.get("seed", [])) == len(FIXTURE["seeds"]), "种子不应在清洗中丢失"
    for r in groups["seed"]:
        assert r["verdict"] != "suspicious", ("误杀", r["text"], r["reasons"])


# ---------- 评级映射边界 ----------

def test_grade_boundaries():
    g = filter_heuristic.grade_from_ratio
    # 0.10 → A（边界取等）
    assert g(20, 2) == "A"
    # 0.10 < r <= 0.25 → B
    assert g(20, 3) == "B"
    assert g(20, 5) == "B"      # 0.25 → B（边界取等）
    # 0.25 < r <= 0.45 → C
    assert g(20, 6) == "C"
    assert g(20, 9) == "C"      # 0.45 → C（边界取等）
    # 0.45 < r <= 0.70 → D
    assert g(20, 10) == "D"
    assert g(20, 14) == "D"     # 0.70 → D（边界取等）
    # r > 0.70 → F
    assert g(20, 15) == "F"
    assert g(100, 71) == "F"
    # 总评论 < 20 → U
    assert g(19, 0) == "U"
    assert g(19, 19) == "U"


# ---------- 统计聚合 ----------

def test_summarize_stats():
    judged = run_pipeline(build_mixed())
    s = filter_heuristic.summarize(judged)
    st = s["stats"]
    assert st["total"] == st["genuine"] + st["suspicious"] + st["gray"]
    assert set(st) == {"total", "genuine", "suspicious", "gray"}
    # fixture 中 genuine 样本带星 → adjusted 非空且仅按 genuine 计算
    assert s["rating"]["original"] is not None
    assert s["rating"]["adjusted"] is not None
    assert s["grade"] in set("ABCDFU")


def test_rating_null_when_no_stars():
    rows = [{"text": "一段没有星数的普通长评价内容", "date": None, "tags": [], "star": None}]
    s = filter_heuristic.summarize(run_pipeline(rows))
    assert s["rating"] == {"original": None, "adjusted": None}


def test_rating_adjusted_uses_genuine_only():
    rows = [
        {"text": "很好", "date": None, "tags": [], "star": 5},                    # suspicious
        {"text": "用了三个月质量依旧很好，大小正合适", "date": None, "tags": [], "star": 3},  # genuine(细节)
    ]
    s = filter_heuristic.summarize(run_pipeline(rows))
    assert s["rating"]["original"] == 4.0
    assert s["rating"]["adjusted"] == 3.0


# ---------- 清洗层 ----------

def test_cleaner_split_and_dedup():
    rows = [
        {"text": "第一段评价\n第二段评价", "date": "2026-09-30", "tags": [], "star": None},
    ]
    cleaned = cleaner.clean_reviews(rows)
    assert [r["text"] for r in cleaned] == ["第一段评价", "第二段评价"]

    rows2 = [
        {"text": "同样的内容", "date": None, "tags": [], "star": None},
        {"text": " 同样的  内容 ", "date": None, "tags": [], "star": None},
    ]
    cleaned2 = cleaner.clean_reviews(rows2)
    assert cleaned2[0]["_dup_key"] == cleaned2[1]["_dup_key"]


def test_cleaner_drops_empty_and_residue():
    rows = [
        {"text": "此用户没有填写评价", "date": None, "tags": [], "star": None},
        {"text": "   ", "date": None, "tags": [], "star": None},
        {"text": "正常评价内容没有问题", "date": None, "tags": [], "star": None},
    ]
    cleaned = cleaner.clean_reviews(rows)
    assert [r["text"] for r in cleaned] == ["正常评价内容没有问题"]
