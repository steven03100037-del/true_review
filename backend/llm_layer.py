# -*- coding: utf-8 -*-
"""LLM 可插拔层——V0 空壳占位，不实现、不调用任何大模型 API。

架构留口子（见 docs/V0_开发需求.md 第 9 节）：
V0 的过滤输出保留 gray 桶；后续版本若引入 LLM，实现本文件签名即可接入，
api/fetcher/filter 各层无需改动。真实 DeepSeek API key 由项目所有人在
config_local.py 自行配置，不经聊天/仓库传递。
"""

from typing import Optional


def judge_gray_reviews(reviews: list) -> list:
    """对 gray 桶评论做 LLM 复判，返回带 verdict/reasons 的新列表。

    V0 不实现。预期契约：入参为过滤管线输出（含 text/tags/verdict 等字段），
    出参同构，verdict ∈ {genuine, suspicious, gray}。
    """
    raise NotImplementedError("V0 不调用 LLM：judge_gray_reviews 未实现")


def rescore_report(report: dict) -> dict:
    """基于 LLM 复判结果重算 stats/rating/grade，返回新报告 dict。

    V0 不实现。预期契约：入参为报告 JSON（字段见需求文档第 3 节），出参同构。
    """
    raise NotImplementedError("V0 不调用 LLM：rescore_report 未实现")


def chat(prompt: str, system: Optional[str] = None) -> str:
    """DeepSeek 对话原语。V0 不实现，key 从 config_local.DEEPSEEK_API_KEY 读取。"""
    raise NotImplementedError("V0 不调用 LLM：chat 未实现")
