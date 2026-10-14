# -*- coding: utf-8 -*-
"""[已废弃 · V0.1] Playwright 抓取模块：链接解析 → 开页 → 旁听评论接口 → 翻页收集。

废弃原因（见 docs/V0.1_扩展改造笔.md 第 0 节）：真实环境验证发现天猫按
"浏览器环境可信度"分级下发评价模块，后端 Playwright（无头/有头/真指纹/
扫码登录/克隆档案+原 cookie 共 6 种组合）全部被静默降级——页面无评价模块。
数据入口已切换为 Chrome 扩展（extension/ 旁听 → POST /api/ingest）。

本文件保留不删除、不参与新调用链，供未来对策参考；
/api/analyze 仍可调用（兼容保留），但前端已不再使用。

原 V0 纪律（留档）：
- 禁止自实现 mtop 签名——签名由页面自身请求自然产生，我们只旁听 network response；
- 接口匹配规则与 JSON 字段路径全部集中在顶部常量区，淘宝改版时只改常量区；
- 失败结构化：AUTH_REQUIRED / NO_RATE_API / TIMEOUT / BAD_URL；
- 单次分析串行执行（模块级锁），无并发队列。
"""

import importlib.util
import random
import re
import threading
import time
from pathlib import Path
from urllib.parse import urlparse, parse_qs

# ==================== 常量区：淘宝改版时只改这里 ====================

# 商品页宿主（host 以其后缀结尾即认可）
ITEM_HOST_SUFFIXES = ("taobao.com", "tmall.com")

# 短链宿主：需要浏览器 302/JS 跳转展开取真实商品 URL
SHORT_LINK_HOSTS = ("m.tb.cn", "tb.cn", "t.tb.cn")

# 商品 id 参数（item.htm?id=xxxx / detail?id=xxxx）
ITEM_ID_PATTERN = r"[?&]id=(\d{6,})"

# 页面最终落在这些 URL 特征上 → 登录态失效
LOGIN_URL_MARKERS = (
    "login.taobao.com", "login.tmall.com", "passport",
    "login.m.taobao.com",
)

# mtop 响应中出现这些字符串 → 会话过期/未登录
SESSION_EXPIRED_MARKERS = (
    "FAIL_SYS_SESSION_EXPIRED", "FAIL_SYS_TOKEN_EXPIRED",
    "SESSION_EXPIRED", "RGV587_WM",
)

# 评论数据接口：响应 URL 粗匹配（xhr/fetch 才看）
RATE_URL_PATTERNS = ("rate", "Rate", "feedback", "comment")

# 评论数据接口：JSON 中列表字段名（递归查找）
RATE_LIST_KEYS = ("rateList", "rateInfo", "rateDetailList", "feedbackList")

# 单条评论的字段路径（同义键依次尝试）
REVIEW_CONTENT_KEYS = ("content", "rateContent")
REVIEW_DATE_KEYS = ("date", "rateDate", "created")
REVIEW_IMAGES_KEYS = ("images", "pics", "photos")
REVIEW_APPEND_KEYS = ("append", "appendComment", "isAppend")
REVIEW_STAR_KEYS = ("star", "rateStar", "score")

# 翻页触发：滚动页面；页间随机等待
PAGE_DEADLINE_SEC = 120        # 单次分析总超时（上限 120s）
GOTO_TIMEOUT_SEC = 30          # 首屏加载超时
SCROLL_WAIT_MIN = 1.0          # 页间 sleep 下限（秒）
SCROLL_WAIT_MAX = 2.0          # 页间 sleep 上限（秒）
MAX_PAGES_DEFAULT = 5          # 默认最多收 5 页
MAX_REVIEWS_DEFAULT = 200      # 或 200 条（先到为准）
NO_NEW_ROUNDS_GIVEUP = 3       # 连续 N 轮无新评论则提前收工
JSON_WALK_MAX_DEPTH = 12       # JSON 递归查找深度上限
SESSION_MARK_SCAN_BYTES = 200_000  # 序列化后扫描会话过期标记的字节上限

# H5 仿真：手机 UA + 视口，让淘宝下发 H5 版页面
MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1"
)
VIEWPORT = {"width": 390, "height": 844}

# ==================== 常量区结束 ====================

_ANALYZE_LOCK = threading.Lock()  # 单次分析串行执行


class FetchError(Exception):
    """结构化抓取失败。code ∈ {AUTH_REQUIRED, NO_RATE_API, TIMEOUT, BAD_URL}。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


# ---------------- 纯函数：链接与 cookie 解析 ----------------

def parse_item_url(url: str) -> str:
    """校验并归一化商品链接，返回可直接打开的 URL。

    接受 item.taobao.com / detail.tmall.com 等淘宝系商品页（须含数字 id），
    以及 m.tb.cn 等短链（原样返回，由浏览器跳转展开）。
    无法识别 → FetchError("BAD_URL")。
    """
    raw = (url or "").strip()
    if not raw:
        raise FetchError("BAD_URL", "链接为空，请粘贴淘宝商品页链接")
    if not re.match(r"^https?://", raw, re.IGNORECASE):
        raw = "https://" + raw
    try:
        parts = urlparse(raw)
    except ValueError:
        raise FetchError("BAD_URL", "链接格式无法解析，请检查后重试")
    host = (parts.hostname or "").lower()
    if not host:
        raise FetchError("BAD_URL", "链接缺少域名，请粘贴完整的淘宝商品页链接")

    if any(host == s or host.endswith("." + s) for s in SHORT_LINK_HOSTS):
        return raw  # 短链：交给浏览器展开

    if any(host == s or host.endswith("." + s) for s in ITEM_HOST_SUFFIXES):
        if re.search(ITEM_ID_PATTERN, raw):
            return raw
        raise FetchError(
            "BAD_URL", "链接里没有找到商品 id，请在商品页复制完整地址后重试"
        )

    raise FetchError("BAD_URL", "只支持淘宝/天猫商品链接，当前链接无法识别")


def is_short_link(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return any(host == s or host.endswith("." + s) for s in SHORT_LINK_HOSTS)


def parse_cookie_string(cookie_str: str) -> list:
    """把浏览器复制的 Cookie 字符串解析为 Playwright cookie 列表。

    同一组键值对分别注入 .taobao.com 与 .tmall.com 两个域（淘宝系跳转均覆盖）。
    非法片段静默跳过。
    """
    cookies = []
    for seg in (cookie_str or "").split(";"):
        seg = seg.strip()
        if not seg or "=" not in seg:
            continue
        name, _, value = seg.partition("=")
        name = name.strip()
        value = value.strip()
        if not name:
            continue
        for domain in (".taobao.com", ".tmall.com"):
            cookies.append(
                {"name": name, "value": value, "domain": domain, "path": "/"}
            )
    return cookies


def _config_local_path() -> Path:
    return Path(__file__).resolve().parent.parent / "config_local.py"


def load_cookie_string() -> str:
    """动态读取 config_local.py 的 TAOBAO_COOKIE；文件/字段缺失返回空串。"""
    path = _config_local_path()
    if not path.is_file():
        return ""
    try:
        spec = importlib.util.spec_from_file_location("true_review_config_local", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    except Exception:
        return ""
    return getattr(module, "TAOBAO_COOKIE", "") or ""


def _should_stop_collecting(
    rounds_no_new: int, rate_hits: int, review_count: int,
    *, max_pages: int, max_reviews: int,
) -> bool:
    """翻页循环终止判定（纯函数）。

    任一条件成立即停：
    - 连续 NO_NEW_ROUNDS_GIVEUP 轮滚动无新评论（翻到底了）；
    - 命中评论接口次数 ≥ max_pages；
    - 已收评论数 ≥ max_reviews。
    """
    if rounds_no_new >= NO_NEW_ROUNDS_GIVEUP:
        return True
    if rate_hits >= max_pages:
        return True
    if review_count >= max_reviews:
        return True
    return False


# ---------------- 抓取主流程 ----------------

def fetch_product_reviews(
    url: str,
    cookie_string: str,
    *,
    max_pages: int = MAX_PAGES_DEFAULT,
    max_reviews: int = MAX_REVIEWS_DEFAULT,
    deadline_sec: int = PAGE_DEADLINE_SEC,
) -> dict:
    """抓取商品标题/主图与评论列表。

    返回 {"product": {"title","image_url"},
          "reviews": [{"text","date","tags","star"}],
          "meta": {"fetched_pages","fetched_count"}}
    失败 raise FetchError。
    """
    open_url = parse_item_url(url)  # 垃圾链接在这里就地拒绝，不启浏览器

    if not (cookie_string or "").strip():
        raise FetchError(
            "AUTH_REQUIRED",
            "尚未配置淘宝 Cookie，请在页面下方「设置」中导入后重试",
        )
    cookies = parse_cookie_string(cookie_string)

    with _ANALYZE_LOCK:  # 串行：同一时刻只有一次抓取
        return _fetch_with_browser(
            open_url, cookies,
            max_pages=max_pages, max_reviews=max_reviews,
            deadline_sec=deadline_sec,
        )


def _fetch_with_browser(open_url, cookies, *, max_pages, max_reviews, deadline_sec) -> dict:
    from playwright.sync_api import sync_playwright  # 延迟导入：服务可先起、后装浏览器

    start = time.monotonic()
    deadline = start + deadline_sec

    def remaining_ms(cap_ms: int = 30_000) -> int:
        left = int((deadline - time.monotonic()) * 1000)
        return max(0, min(left, cap_ms))

    def expired() -> bool:
        return time.monotonic() >= deadline

    raw_reviews: list = []          # {"text","date","tags","star"}
    _seen_texts: set = set()
    state = {"rate_hits": 0, "login_expired": False, "login_page": False}

    def add_review(text, date, tags, star):
        text = (text or "").strip()
        if not text or text in _seen_texts:
            return
        _seen_texts.add(text)
        raw_reviews.append(
            {"text": text, "date": date or None,
             "tags": tags or [], "star": star}
        )

    def extract_first(item: dict, keys):
        for k in keys:
            if k in item and item[k] is not None:
                return item[k]
        return None

    def walk_rate_lists(node, depth=0):
        """递归找出所有命中 RATE_LIST_KEYS 的列表（元素须为 dict）。"""
        found = []
        if depth > JSON_WALK_MAX_DEPTH:
            return found
        if isinstance(node, dict):
            for k, v in node.items():
                if k in RATE_LIST_KEYS and isinstance(v, list):
                    found.extend(x for x in v if isinstance(x, dict))
                else:
                    found.extend(walk_rate_lists(v, depth + 1))
        elif isinstance(node, list):
            for x in node:
                found.extend(walk_rate_lists(x, depth + 1))
        return found

    def on_response(response):
        """旁听响应：命中评论接口则提取评论；绝不抛异常。"""
        try:
            if expired():
                return
            if response.request.resource_type not in ("xhr", "fetch"):
                return
            url_l = (response.url or "").lower()
            if url_l.startswith("data:"):
                return
            body = response.json()  # 非 JSON 会抛，下面接住
            marker_scan = str(body)[:SESSION_MARK_SCAN_BYTES]
            if any(m in marker_scan for m in SESSION_EXPIRED_MARKERS):
                state["login_expired"] = True
            url_hit = any(p.lower() in url_l for p in RATE_URL_PATTERNS)
            items = walk_rate_lists(body)
            if not items and not url_hit:
                return
            before = len(raw_reviews)
            for item in items:
                content = extract_first(item, REVIEW_CONTENT_KEYS)
                if not content:
                    continue
                images = extract_first(item, REVIEW_IMAGES_KEYS)
                tags = []
                if images:
                    tags.append("带图")
                if extract_first(item, REVIEW_APPEND_KEYS):
                    tags.append("追评")
                star = extract_first(item, REVIEW_STAR_KEYS)
                try:
                    star = float(star) if star is not None else None
                except (TypeError, ValueError):
                    star = None
                add_review(
                    content,
                    extract_first(item, REVIEW_DATE_KEYS),
                    tags, star,
                )
            if len(raw_reviews) > before:
                state["rate_hits"] += 1
        except Exception:
            return  # 旁听失败不影响主流程

    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(headless=True)
        except Exception as exc:
            raise FetchError(
                "TIMEOUT",
                f"浏览器启动失败：请先运行 playwright install chromium（{exc}）",
            )
        try:
            context = browser.new_context(
                user_agent=MOBILE_UA, viewport=VIEWPORT, is_mobile=True,
                locale="zh-CN",
            )
            if cookies:
                try:
                    context.add_cookies(cookies)
                except Exception:
                    pass  # 个别 cookie 非法不阻断
            page = context.new_page()
            page.on("response", on_response)

            try:
                page.goto(open_url, wait_until="domcontentloaded",
                          timeout=remaining_ms(GOTO_TIMEOUT_SEC * 1000))
            except Exception:
                if expired():
                    raise FetchError("TIMEOUT", "打开商品页超时，请稍后再试")
                raise FetchError("NO_RATE_API", "商品页加载失败，请稍后再试")

            final_url = page.url
            if is_short_link(final_url):
                # 短链展开等待：跳转未完成就再等一会儿
                page.wait_for_load_state("domcontentloaded",
                                         timeout=remaining_ms(10_000))
                final_url = page.url
            if any(m in final_url for m in LOGIN_URL_MARKERS):
                state["login_page"] = True
            if is_short_link(final_url) or not re.search(ITEM_ID_PATTERN, final_url):
                raise FetchError(
                    "BAD_URL", "短链展开后不是有效的商品页，请重新复制链接"
                )

            # 商品标题/主图：OG meta
            product = {"title": None, "image_url": None}
            try:
                product["title"] = page.eval_on_selector(
                    'meta[property="og:title"]', "el => el.content"
                ) or page.title()
                product["image_url"] = page.eval_on_selector(
                    'meta[property="og:image"]', "el => el.content"
                )
            except Exception:
                product["title"] = product["title"] or page.title()

            # 翻页：滚动触发加载，页间随机 1~2s；5 页或 200 条先到为准
            no_new_rounds = 0
            while not _should_stop_collecting(
                no_new_rounds, state["rate_hits"],
                len(raw_reviews), max_pages=max_pages, max_reviews=max_reviews,
            ):
                if expired():
                    break  # 到点收工：已有数据在收尾正常返回，无数据才按 TIMEOUT
                prev_count = len(raw_reviews)
                try:
                    page.mouse.wheel(0, 4000)
                    page.evaluate("window.scrollBy(0, document.body.scrollHeight)")
                except Exception:
                    pass
                page.wait_for_timeout(
                    int(random.uniform(SCROLL_WAIT_MIN, SCROLL_WAIT_MAX) * 1000)
                )
                if len(raw_reviews) > prev_count:
                    no_new_rounds = 0    # 该轮有新评论，计数归零
                else:
                    no_new_rounds += 1   # 该轮无增长

            timed_out = expired()  # 超时但已抓到数据：返回已有部分，不丢弃
            if state["login_page"] or (
                state["login_expired"] and not raw_reviews
            ):
                raise FetchError(
                    "AUTH_REQUIRED",
                    "淘宝登录已过期或无效，请在页面下方「设置」中更新 Cookie",
                )
            if not raw_reviews:
                if timed_out:
                    raise FetchError("TIMEOUT", "抓取评论超时，请稍后再试")
                raise FetchError(
                    "NO_RATE_API",
                    "没有抓到评论数据（接口未命中或该商品没有评论）",
                )

            return {
                "product": product,
                "reviews": raw_reviews,
                "meta": {
                    "fetched_pages": min(state["rate_hits"], max_pages),
                    "fetched_count": len(raw_reviews),
                    **({"truncated_by_timeout": True} if timed_out else {}),
                },
            }
        finally:
            browser.close()
