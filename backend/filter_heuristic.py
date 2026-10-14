# -*- coding: utf-8 -*-
"""规则过滤主体：纯函数、零外部依赖，所有阈值集中在顶部常量区（调参不改逻辑）。

规则（见 docs/V0_开发需求.md 第 5 节）：
  1 清洗后精确去重 + 空白规范化去重            → suspicious  "重复评论"
  2 去标点后长度 ≤6 且命中好评词典              → suspicious  "超短模板好评"
  3 3-gram shingle + Jaccard ≥0.8 聚簇，簇 ≥3  → 全簇 suspicious  "与同簇N条高度相似"
  4 同一前后缀模板不同填充词 ≥3 条              → suspicious  "模板化刷单"
  5 带图 / 追评 / 长度≥15 且含细节词            → genuine     "带图实拍"/"追评"/"细节描述"
  6 其余                                       → gray（V0 报告单列，不自动判死）

判定优先级：1–4 任一命中 → suspicious（reasons 累积，可同时含规则 5 的正向理由）；
否则规则 5 命中 → genuine；否则 gray。
"""

import re
from collections import Counter

# ==================== 常量区：调参只改这里 ====================

SHORT_LEN_MAX = 6            # 规则 2：去标点后长度上限
PRAISE_WORDS = (             # 规则 2：好评词典（子串命中）
    "很好", "不错", "可以", "还行", "好评", "满意", "棒",
    "一般", "赞", "超值", "划算", "喜欢",
)

JACCARD_THRESHOLD = 0.8      # 规则 3：shingle 相似度阈值
CLUSTER_MIN = 3              # 规则 3：成簇最少条数

TEMPLATE_COMMON_LEN = 6      # 规则 4：公共前/后缀最短长度
TEMPLATE_GROUP_MIN = 3       # 规则 4：同模板分组最少条数

DETAIL_LEN_MIN = 15          # 规则 5：细节描述的最短长度
DETAIL_WORDS = (             # 规则 5：细节词（子串命中）
    "用了", "穿着", "吃", "个月", "质量", "大小", "尺寸", "手感",
    "物流", "包装", "客服", "做工", "舒适", "续航", "味道", "颜色",
)
GENUINE_TAG_REASONS = {"带图": "带图实拍", "追评": "追评"}

# 评级映射（按 suspicious 占比，逐档 ≤ 判定）
GRADE_BANDS = ((0.10, "A"), (0.25, "B"), (0.45, "C"), (0.70, "D"))
GRADE_FALLBACK = "F"         # 占比 > 最后一档上限
GRADE_MIN_TOTAL = 20         # 总评论低于此数 → U（数据不足）

# ==================== 常量区结束 ====================

_WS_RE = re.compile(r"\s+")
_PUNCT_RE = re.compile(r"[\W_]+", re.UNICODE)

SUSPICIOUS_FIXED = ("重复评论", "超短模板好评", "模板化刷单")
SUSPICIOUS_PREFIX = ("与同簇",)


def strip_punct(text: str) -> str:
    """去标点/符号/空白，只留文字数字。"""
    return _PUNCT_RE.sub("", str(text or ""))


def normalize_ws(text: str) -> str:
    return _WS_RE.sub(" ", str(text or "")).strip()


def shingles3(text: str) -> set:
    """3-gram 字符 shingle 集合。"""
    t = _WS_RE.sub("", str(text or ""))
    if len(t) < 3:
        return {t} if t else set()
    return {t[i:i + 3] for i in range(len(t) - 2)}


def jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


# ---------------- 规则实现 ----------------

def _rule1_dup_keys(rows):
    """规则 1：依据 cleaner 的 _dup_key 标记重复（首现不算，其后都算）。"""
    reasons = [[] for _ in rows]
    first_seen = {}
    for i, r in enumerate(rows):
        key = r.get("_dup_key") or _WS_RE.sub("", r.get("text") or "")
        if key and key in first_seen:
            reasons[i].append("重复评论")
        else:
            first_seen[key] = i
    return reasons


def _rule2_short_praise(rows):
    """规则 2：超短模板好评。"""
    reasons = []
    for r in rows:
        t = strip_punct(r.get("text"))
        hit = bool(t) and len(t) <= SHORT_LEN_MAX and any(w in t for w in PRAISE_WORDS)
        reasons.append(["超短模板好评"] if hit else [])
    return reasons


def _rule3_cluster(rows):
    """规则 3：3-gram shingle Jaccard 种子聚簇（保序贪心），簇 ≥3 全簇标疑。"""
    n = len(rows)
    sh = [shingles3(r.get("text")) for r in rows]
    cluster_id = [-1] * n
    cid = 0
    for i in range(n):
        if cluster_id[i] != -1 or not sh[i]:
            continue
        cluster_id[i] = cid
        for j in range(i + 1, n):
            if cluster_id[j] == -1 and sh[j] and jaccard(sh[i], sh[j]) >= JACCARD_THRESHOLD:
                cluster_id[j] = cid
        cid += 1
    sizes = Counter(c for c in cluster_id if c != -1)
    return [
        [f"与同簇{sizes[cluster_id[i]] - 1}条高度相似"]
        if cluster_id[i] != -1 and sizes[cluster_id[i]] >= CLUSTER_MIN else []
        for i in range(n)
    ]


def _rule4_template(rows):
    """规则 4：公共前后缀 ≥6 字且不重叠 → 同模板；分组 ≥3 标疑（并查集）。"""
    n = len(rows)
    comp = [_WS_RE.sub("", r.get("text") or "") for r in rows]
    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i in range(n):
        for j in range(i + 1, n):
            a, b = comp[i], comp[j]
            m = min(len(a), len(b))
            p = 0
            while p < m and a[p] == b[p]:
                p += 1
            if p < TEMPLATE_COMMON_LEN:
                continue
            s = 0
            while s < m and a[-1 - s] == b[-1 - s]:
                s += 1
            if s < TEMPLATE_COMMON_LEN:
                continue
            if p + s > m:
                continue  # 前后缀重叠 = 内容几乎相同，归规则 1/3 管
            ri, rj = find(i), find(j)
            if ri != rj:
                parent[rj] = ri

    sizes = Counter(find(i) for i in range(n))
    return [["模板化刷单"] if sizes[find(i)] >= TEMPLATE_GROUP_MIN else [] for i in range(n)]


def _rule5_positive(rows):
    """规则 5：带图 / 追评 / 细节描述。"""
    reasons = []
    for r in rows:
        out = []
        for tag, why in GENUINE_TAG_REASONS.items():
            if tag in (r.get("tags") or []):
                out.append(why)
        t = _WS_RE.sub("", r.get("text") or "")
        if len(t) >= DETAIL_LEN_MIN and any(w in t for w in DETAIL_WORDS):
            out.append("细节描述")
        reasons.append(out)
    return reasons


def filter_reviews(reviews: list) -> list:
    """给每条评论加 verdict ∈ {genuine, suspicious, gray} 与 reasons[]。

    不改入参：返回带标注的新列表。
    """
    rows = [dict(r) for r in (reviews or [])]
    n = len(rows)
    reasons = [[] for _ in range(n)]
    for produced in (
        _rule1_dup_keys(rows), _rule2_short_praise(rows),
        _rule3_cluster(rows), _rule4_template(rows), _rule5_positive(rows),
    ):
        for i, item in enumerate(produced):
            reasons[i].extend(item)

    for i, r in enumerate(rows):
        rs = reasons[i]
        suspicious = any(
            x in SUSPICIOUS_FIXED or x.startswith(SUSPICIOUS_PREFIX) for x in rs
        )
        r["reasons"] = rs
        r["verdict"] = "suspicious" if suspicious else ("genuine" if rs else "gray")
    return rows


# ---------------- 统计聚合 ----------------

def grade_from_ratio(total: int, suspicious: int) -> str:
    """评级映射：≤10%→A ≤25%→B ≤45%→C ≤70%→D >70%→F；总数 <20 → U。"""
    if total < GRADE_MIN_TOTAL:
        return "U"
    ratio = suspicious / total
    for cap, grade in GRADE_BANDS:
        if ratio <= cap:
            return grade
    return GRADE_FALLBACK


def summarize(reviews: list) -> dict:
    """占比统计 + 评级 + 原始/真实均分（无星数则对应字段 null）。"""
    rows = reviews or []
    counts = {"genuine": 0, "suspicious": 0, "gray": 0}
    stars_all, stars_genuine = [], []
    for r in rows:
        v = r.get("verdict", "gray")
        counts[v] = counts.get(v, 0) + 1
        s = r.get("star")
        if isinstance(s, (int, float)) and s > 0:
            stars_all.append(float(s))
            if v == "genuine":
                stars_genuine.append(float(s))
    total = len(rows)
    rating = {
        "original": round(sum(stars_all) / len(stars_all), 1) if stars_all else None,
        "adjusted": round(sum(stars_genuine) / len(stars_genuine), 1) if stars_genuine else None,
    }
    return {
        "stats": {"total": total, **counts},
        "grade": grade_from_ratio(total, counts["suspicious"]),
        "rating": rating,
    }
