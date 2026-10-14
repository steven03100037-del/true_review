# 「真评」V0 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 粘贴淘宝链接 → Playwright 抓评论 → 规则过滤水军 → 手机可看的"真实评论报告"，V0 零 LLM 依赖。

**Architecture:** FastAPI 单进程同步服务；`/api/analyze` 在线程池内串行跑 Playwright（旁听评论接口），数据流 fetcher → cleaner → filter_heuristic → api 组装报告 JSON；前端单文件 index.html 原生 JS 轮询式提交/展示。过滤与评级为纯函数，全部阈值集中常量区。

**Tech Stack:** Python 3.10 / FastAPI / uvicorn(0.0.0.0:8000) / Playwright(sync API, chromium headless) / pydantic v2 / pytest；前端零依赖原生 HTML+CSS+JS。

## Global Constraints

- 报告 JSON 字段名与 `docs/V0_开发需求.md` 第 3 节逐字一致，一个不改
- 错误码集合：`AUTH_REQUIRED | NO_RATE_API | TIMEOUT | BAD_URL`（message 中文）
- 服务绑定 `0.0.0.0:8000`；`/` 服务 `web/index.html`
- 凭据绝不入库：`config_local.py` 进 `.gitignore`，仓库只放 `config_local.example.py`；开发期占位 cookie
- 禁止：自实现 mtop 签名、批量/并发抓取、账号池、LLM 调用、数据库、其他平台（第 9 节清单全部不做）
- 架构留口子：gray 桶保留；`llm_layer.py` 只建空壳签名
- `filter_heuristic.py` 纯函数零外部依赖，阈值集中在顶部常量区
- 接口匹配规则与 JSON 字段路径集中在 `fetcher.py` 顶部常量区
- git：本地 init，feature 分支开发 → 合 main；推 GitHub 由项目所有人完成（无凭据）

## File Structure

| 文件 | 职责 |
|---|---|
| `backend/fetcher.py` | 链接解析、cookie 加载、Playwright 抓取、评论接口旁听、翻页、错误码 |
| `backend/cleaner.py` | 切分多行、去 UI 残留、空白规范化、精确重复标记 |
| `backend/filter_heuristic.py` | 6 条规则判定 + reasons + stats/rating/grade 聚合（纯函数） |
| `backend/llm_layer.py` | 空壳：可插拔函数签名，`NotImplementedError` |
| `backend/api.py` | 4 个路由、报告 JSON 组装、config_local 写入、串行锁 |
| `web/index.html` | 单文件页面，手机 Safari 优先 |
| `tests/test_filter.py` + `tests/fixtures/comments_fake.json` | 过滤管线 TDD，零外部依赖 |
| `config_local.example.py` / `requirements.txt` / `.gitignore` / `start.bat` / `README.md` | 交付物 |

内部评论模型（管线传递的 dict）：`{"text": str, "date": str|None, "tags": list[str](⊂{带图,追评}), "star": float|None, "_case": str}`（`_case` 仅测试用，产出前剥除）。

---

### Task 1: 项目骨架 + 依赖基线

**Files:** Create `requirements.txt`, `.gitignore`, `config_local.example.py`, `config_local.py`(占位，gitignore), `backend/`(空目录), `web/`, `tests/fixtures/`; git init + feature/v0 分支。

- [ ] requirements.txt：fastapi / uvicorn / playwright / pydantic / pytest（宽松下限版本）
- [ ] .gitignore：`__pycache__/`、`*.log`、`pw-profile/`、`.venv/`、`config_local.py`、`.pytest_cache/`
- [ ] config_local.example.py：`TAOBAO_COOKIE = ""` + `DEEPSEEK_API_KEY = ""`（中文注释：如何填）
- [ ] 本机 `config_local.py`：占位 cookie `cookie2=PLACEHOLDER_NOT_REAL; t=PLACEHOLDER`（语法合法即可）
- [ ] git init、分支 feature/v0、首次提交

### Task 2: fetcher.py（最险，优先打通）

**Files:** Create `backend/fetcher.py`

**Interfaces (Produces):**
- `class FetchError(Exception)`：`.code` ∈ 四错误码，`.message` 中文
- `parse_item_url(url: str) -> str`：识别 taobao/tmall 商品页 / m.tb.cn 短链标记，提取数字 item id；不识别 → `raise FetchError("BAD_URL", ...)`
- `parse_cookie_string(cookie: str) -> list[dict]`：`"k=v; k2=v2"` → playwright cookie dicts（domain `.taobao.com` 与 `.tmall.com` 各一份，path=/）
- `load_cookie_string() -> str`：读项目根 `config_local.py` 的 `TAOBAO_COOKIE`（importlib 动态加载，文件缺失/字段缺 → ""）
- `fetch_product_reviews(url: str, cookie_string: str, *, max_pages=5, max_reviews=200, deadline_sec=120) -> dict`：返回 `{"product": {"title","image_url"}, "reviews": [{"text","date","tags","star"}], "meta": {"fetched_pages","fetched_count"}}`；playwright 延迟导入

**顶部常量区（改版只改这里）：**
```python
ITEM_HOST_SUFFIXES = ("taobao.com", "tmall.com")      # 商品页宿主
SHORT_LINK_HOSTS = ("m.tb.cn", "tb.cn", "t.tb.cn")    # 短链 302 展开
LOGIN_URL_MARKERS = ("login.taobao.com", "login.tmall.com", "passport", "login.m.taobao.com")
SESSION_EXPIRED_MARKERS = ("FAIL_SYS_SESSION_EXPIRED", "FAIL_SYS_TOKEN_EXPIRED", "SESSION_EXPIRED", " RGV587_WM")
RATE_URL_PATTERNS = ("rate", "Rate", "feedback", "comment")   # 响应 URL 粗匹配
RATE_LIST_KEYS = ("rateList", "rateInfo", "rateDetailList", "feedbackList")  # JSON 命中
REVIEW_CONTENT_KEYS = ("content", "rateContent")
REVIEW_DATE_KEYS = ("date", "rateDate", "created")
REVIEW_IMAGES_KEYS = ("images", "pics", "photos")
REVIEW_APPEND_KEYS = ("append", "appendComment")
REVIEW_STAR_KEYS = ("star", "rateStar", "score")
PAGE_TIMEOUT_MS / SCROLL_WAIT_MIN / SCROLL_WAIT_MAX / DEADLINE_SEC = 120 ...
```

- [ ] 步骤 1：常量区 + 三个纯函数（parse_item_url / parse_cookie_string / load_cookie_string）+ FetchError，`python -c` 冒烟导入与解析
- [ ] 步骤 2：`fetch_product_reviews`：串行锁 → 空 cookie 直接 `AUTH_REQUIRED`（不启浏览器，快速路径）→ playwright sync API：注入 cookie → goto（短链先 goto 展开 final URL 再校验，仍不识别 → BAD_URL）→ final URL 命中 LOGIN_URL_MARKERS → AUTH_REQUIRED；`page.on("response")` 旁听：resource_type ∈ {xhr, fetch} 且 URL 命中 RATE_URL_PATTERNS，或响应 JSON 顶层/嵌套命中 RATE_LIST_KEYS → `_extract_reviews(json)`；响应 JSON 任一层含 SESSION_EXPIRED_MARKERS → 置 login_expired 标记；OG meta（`og:title`/`og:image`）取标题主图；循环：滚动触发加载 → `random.uniform(1,2)` 秒 sleep → 收集去重（按 text 幂等）→ 达 max_pages/max_reviews 或 deadline（`time.monotonic`，剩余时间喂给 playwright timeout）→ 退出；一条评论接口都没命中且无登录跳转 → NO_RATE_API；deadline 到 → TIMEOUT
- [ ] 步骤 3：导入冒烟 + `parse_item_url` 单元行为手验（合法/短链/垃圾 URL 三例，垃圾 URL 断言 BAD_URL，不开浏览器）

### Task 3: fixtures + test_filter.py（先写测试，确认失败）

**Files:** Create `tests/fixtures/comments_fake.json`, `tests/test_filter.py`

**fixture 结构**（`_case` 标记身份，测试据此断言）：
```json
{"seeds": [30 条真实感评论], "spam": {"short": [8], "cluster": [6], "template": [6]}}
```
- seeds 30 条：15–60 字自然中文、互不相同、不以好评词典词收尾开头、部分 tags 带图/追评 → 期望 genuine 或 gray，零误杀
- short 8 条：≤6 字（去标点后）且命中好评词典，其中 2 条重复 "很好"（顺带覆盖规则 1）→ 全部 suspicious
- cluster 6 条：同一 25 字句换 1–2 字变体（3-gram Jaccard ≥0.8 成簇）→ 全簇 suspicious
- template 6 条：同 6 字前缀 + 6 字后缀、中间填充差异大到 Jaccard <0.8 → 全部 suspicious

**test_filter.py 断言清单：**
1. `test_spam_all_caught`：混排（固定顺序模拟）后 short/cluster/template 全部 verdict=="suspicious"
2. `test_short_reasons`：short 条目 reasons 含 "超短模板好评"；重复的 "很好" 另含 "重复评论"
3. `test_cluster_reasons`：cluster 每条 reasons 含 "与同簇" 前缀文案
4. `test_template_reasons`：template 每条 reasons 含 "模板化刷单"
5. `test_seeds_not_killed`：30 条 seed 无一 suspicious
6. `test_grade_boundaries`：`grade_from_ratio` 边界——(10,1)A、(20,2)A、(20,3)B、(20,5)B、(20,9)C、(20,10)D、(20,14)D、(20,15)F、(19,0)U、(19,19)U
7. `test_summarize_stats`：stats 四数之和==total；rating 无星 → 双 null；有 genuine 星 → adjusted 仅按 genuine 均分
8. `test_cleaner_split_and_dedup`：多行文本切分；空白规范化去重标记（cleaner 层）
- 测试文件头部 `sys.path.insert(0, 项目根)`，测试零外部依赖（不碰网/LLM/playwright）
- [ ] 运行 `python -m pytest tests/ -v` → 预期全 FAIL（模块不存在）

### Task 4: cleaner.py + filter_heuristic.py（最小实现到全绿）

**Files:** Create `backend/cleaner.py`, `backend/filter_heuristic.py`

**cleaner.py（Produces）**：
- `UI_RESIDUE_PATTERNS` 常量（"举报"/"有用"/"回复"/"查看更多"等）
- `clean_text(text) -> str`：去 UI 残留片段、`[\n\r]+` 切分为多条、折叠空白、strip
- `clean_reviews(reviews: list[dict]) -> list[dict]`：逐条切分展开、空文本丢弃、`_dup_key = 去空白规范化 text`（供规则 1 判精确/规范化重复）

**filter_heuristic.py（Produces，纯函数）**：
- 顶部常量区：`SHORT_LEN_MAX=6`、`PRAISE_WORDS=(很好,不错,可以,还行,好评,满意,棒,一般,赞,超值,划算,喜欢)`、`JACCARD_THRESHOLD=0.8`、`CLUSTER_MIN=3`、`TEMPLATE_COMMON_LEN=6`、`TEMPLATE_GROUP_MIN=3`、`DETAIL_LEN_MIN=15`、`DETAIL_WORDS=(用了,穿着,吃,个月,质量,大小,尺寸,手感,物流,包装,客服,做工,舒适,续航,味道,颜色)`、`GRADE_BANDS=((0.10,"A"),(0.25,"B"),(0.45,"C"),(0.70,"D"))`、`GRADE_MIN_TOTAL=20`
- `strip_punct(text) -> str`、`normalize_ws(text) -> str`、`shingles3(text) -> set`
- `filter_reviews(reviews) -> list[dict]`：就地加 `verdict`/`reasons`；优先级 1–4 → suspicious（reasons 累积），否则规则 5 → genuine，否则 gray；规则 1 依据 `_dup_key` 首现重复（含规范化重复）→ "重复评论"；规则 2 去标点长度 ≤6 且 PRAISE_WORDS 子串命中 → "超短模板好评"；规则 3 shingle Jaccard ≥0.8 种子聚簇（保序贪心，簇 ≥3）→ f"与同簇{N}条高度相似"；规则 4 两两 common prefix ≥6 且 common suffix ≥6 且不重叠、并查集分组 ≥3 → "模板化刷单"；规则 5 tags 含带图/追评 → "带图实拍"/"追评"，或长度 ≥15 且含细节词 → "细节描述"
- `grade_from_ratio(total: int, suspicious: int) -> str`：total<20 → "U"；ratio=susp/total 逐档 ≤ → A/B/C/D，>0.70 → "F"
- `summarize(reviews) -> dict`：`{"stats": {"total","genuine","suspicious","gray"}, "grade": str, "rating": {"original": float|None, "adjusted": float|None}}`（star 全缺失 → 双 null；adjusted 仅 genuine 含星均分）

- [ ] TDD 循环：跑测试 → 逐规则实现 → 全绿（8 个测试）
- [ ] 手验：`_case` 等内部键不影响判定；threshold 全部来自常量区

### Task 5: llm_layer.py 空壳 + api.py

**Files:** Create `backend/llm_layer.py`, `backend/api.py`

**llm_layer.py**：`judge_gray_reviews(reviews)` / `rescore_report(report)` / `chat(prompt, system)` 三个签名 + docstring "V0 不调用"，`raise NotImplementedError`。

**api.py（Produces：HTTP 契约）**：
- `AnalyzeRequest(BaseModel)`：`url: str`
- `GET /api/health` → `{"ok": true}`
- `POST /api/analyze`：取 cookie（空 → 报告形 AUTH_REQUIRED）→ `fetcher.fetch_product_reviews`（FetchError → 错误形报告）→ `cleaner.clean_reviews` → `filter_heuristic.filter_reviews` + `summarize` → 组装报告 JSON（字段名照契约；`reviews` = genuine+gray 按 追评>带图>细节>普通 排序、id 1 起、剥 `_case`/`_dup_key`/`star`；`suspicious` 数组仅 {text,date,reasons}；`meta.elapsed_sec`；`error: None`）；未预期异常 → `{"error": {"code": "INTERNAL", "message": 中文}}`（HTTP 200，前端兜底）
- 错误形报告：同一顶层结构，数据字段置空/null，`error` 填 `{code, message}`
- `POST /api/settings/cookie`：body `{"cookie": str}` → 保留旧 `DEEPSEEK_API_KEY` 重写 `config_local.py`（repr 转义）→ `{"ok": true}`
- `GET /` → `FileResponse(web/index.html)`
- 端口绑定由启动命令控制（uvicorn 0.0.0.0:8000）

- [ ] 起 uvicorn（后台）→ curl /api/health == `{"ok":true}`；curl / 含 `<title>`
- [ ] 临移 config_local.py → POST /api/analyze `{"url":"https://item.taobao.com/item.htm?id=123"}` → `AUTH_REQUIRED` → 还原文件

### Task 6: web/index.html

**Files:** Create `web/index.html`

区块（单文件、无依赖、viewport meta、正文字号 ≥16px、按钮 ≥44px 高）：
- 输入区：textarea（粘贴链接）+ 大按钮「分析」；分析中 spinner + "正在打开商品页抓取评论，约 20-60 秒"，按钮禁用
- 报告卡：商品标题+主图；大字母评级（A绿 B蓝 C黄 D橙 F红 U灰）；★original → ★adjusted（null 隐藏整行）；三段占比条（绿=真实/黄=可疑/灰=边界，带数字）
- tab×3：真实评论（genuine 按 追评>带图>细节>普通 排序——后端已排，前端保序；每条 tags chip，gray 条目置灰加「边界」chip 追加在 genuine 之后）｜疑似水军（reasons 红字）｜数据（meta 四项）
- 设置区：`<details>` 默认折叠；textarea 粘贴 cookie + 保存按钮 → POST /api/settings/cookie，成功提示
- 错误映射：AUTH_REQUIRED→"登录已过期，请在下方设置更新 Cookie"并 `details.open=true`；BAD_URL/NO_RATE_API/TIMEOUT/其他→各配中文人话

- [ ] 手机宽度（390px DevTools 视口）自检布局无横向滚动

### Task 7: start.bat + README.md

**Files:** Create `start.bat`, `README.md`

- start.bat：`chcp 65001` → cd 脚本目录 → 无 `.venv` 则 `python -m venv .venv` + pip 装 requirements + `playwright install chromium`（已装则秒过）→ `.venv\Scripts\python -m uvicorn backend.api:app --host 0.0.0.0 --port 8000`
- README.md：项目一句话；起服务三步（1 双击 start.bat / 2 首次自动装环境等它跑完 / 3 手机同 WiFi Safari 开 `http://电脑IP:8000`，附 `ipconfig` 查 IP 法）；cookie 导入小白教程（电脑 Chrome 开 taobao 并登录 → F12 → Network 刷新 → 任一请求 → Request Headers → Cookie 整串复制 → 页面设置区粘贴保存；或 F12 Application → Cookies）；常见错误码对照表；边界说明（第 9 节）

- [ ] 文档自查：三步齐、cookie 教程到按键级

### Task 8: 端到端验收 + git 合并

- [ ] `python -m pytest tests/ -v` 全绿（贴输出）
- [ ] 起服务冒烟：health ok、/ 出页、无 cookie → AUTH_REQUIRED（贴输出）
- [ ] 占位 cookie 全链路冒烟（浏览器真开 taobao → 登录态检查 → AUTH_REQUIRED 或 NO_RATE_API，贴结果；网络不通则记录为待用户自测项）
- [ ] feature/v0 → main 合并（--no-ff），`git log` 确认；推 GitHub 留给项目所有人（README/自检报告注明）

---

## Self-Review 结论

- 覆盖检查：第 2 节目录↔Task 1–7；第 3 节契约↔Task 5/6；第 4 节↔Task 2；第 5 节↔Task 3/4；第 6 节↔Task 6；第 7 节验收↔Task 8；第 8 节交付↔Task 1/7/8；第 9 节不做↔Global Constraints；第 10 节凭据↔Task 1 占位 cookie + Global Constraints。无缺口。
- 占位扫描：无 TBD/TODO；index.html 按区块规格实现而非占位描述。
- 类型一致性：`FetchError(code,message)`、`fetch_product_reviews` 返回 dict 形状、`filter_reviews`/`summarize`/`grade_from_ratio` 签名在各任务间已对齐；报告字段名以第 3 节逐字为准。
