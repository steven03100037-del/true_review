# -*- coding: utf-8 -*-
"""清洗层：切分多行、去 UI 残留、空白规范化、重复标记。

职责边界：cleaner 只做数据准备（清理 + 打 _dup_key 标记），
重复的"判定"由 filter_heuristic 规则 1 完成。
"""

import re

# UI 残留片段：整段剔除（都是界面文案，不会出现在正常评语句中）
UI_RESIDUE_PATTERNS = (
    "此用户没有填写评价",
    "默认好评",
    "查看更多评价",
    "查看全部评价",
    "晒单有礼",
    "举报",
)

_SPLIT_RE = re.compile(r"[\n\r]+")
_WS_RE = re.compile(r"\s+")


def clean_text(text: str) -> str:
    """去 UI 残留、折叠空白、去首尾空白。"""
    t = str(text or "")
    for pat in UI_RESIDUE_PATTERNS:
        t = t.replace(pat, "")
    return _WS_RE.sub(" ", t).strip()


def clean_reviews(reviews: list) -> list:
    """清洗评论列表。

    - 单条评论按换行切分为多条（初评+追评挤在一个字段的情况）；
    - 清洗后为空的丢弃；
    - 每条打上 _dup_key（去掉全部空白后的文本，供精确/空白规范化去重）；
    - 其余字段（date/tags/star/_case 等）原样透传。
    """
    out = []
    for r in reviews or []:
        base = dict(r)
        pieces = [p for p in _SPLIT_RE.split(str(base.get("text") or "")) if p.strip()]
        for piece in pieces:
            text = clean_text(piece)
            if not text:
                continue
            item = dict(base)
            item["text"] = text
            item["_dup_key"] = _WS_RE.sub("", text)
            out.append(item)
    return out
