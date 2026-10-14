# -*- coding: utf-8 -*-
"""fetcher 纯函数回归测试——零外部依赖（不开浏览器、不碰网）。

回归背景：翻页循环曾把「连续无新评论」的计数限定在 len(raw_reviews)==0 时，
已抓到部分评论后循环无法自然终止，空转到 120s deadline 抛 TIMEOUT 并丢弃
已抓数据。修复后终止判定抽为纯函数 _should_stop_collecting，本文件锁定其行为。
"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend import fetcher  # noqa: E402


def test_stop_after_giveup_rounds_with_data():
    """a) 连续 NO_NEW_ROUNDS_GIVEUP 轮无新评论且已抓到数据 → 停止收集。

    （修复前：此场景会空转到超时并丢弃已抓的 40 条。）
    """
    assert fetcher._should_stop_collecting(
        fetcher.NO_NEW_ROUNDS_GIVEUP, 1, 40, max_pages=5, max_reviews=200
    ) is True


def test_keep_going_when_round_had_new():
    """b) 该轮有新评论（rounds_no_new 归零）→ 继续收集。"""
    assert fetcher._should_stop_collecting(
        0, 1, 40, max_pages=5, max_reviews=200
    ) is False


def test_stop_when_review_cap_reached():
    """c) 已收条数达上限 → 停止收集。"""
    assert fetcher._should_stop_collecting(
        0, 2, 200, max_pages=5, max_reviews=200
    ) is True
