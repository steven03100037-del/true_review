# 真评 · V0

粘贴淘宝商品链接 → 自动抓评论 → 规则过滤水军 → 出一页手机能看的「真实评论报告」。
个人家庭使用工具，**V0 零 LLM 依赖**，不上架、不部署公网。

---

## 一、起服务（三步）

> 前提：Windows 电脑装好 Python 3.10+（[python.org](https://www.python.org/downloads/) 安装时勾选 **Add Python to PATH**）。

1. **双击 `start.bat`**
   首次运行会自动创建虚拟环境、安装依赖、下载浏览器内核（几分钟，只需要这一次）。
2. **等到窗口出现这一行，说明服务已启动：**
   ```
   Uvicorn running on http://0.0.0.0:8000
   ```
   电脑浏览器打开 `http://127.0.0.1:8000` 能看到「真评」页面即成功。**这个黑窗口别关**，关了服务就停。
3. **手机访问**：iPhone 连**同一个 WiFi**，Safari 打开 `http://电脑IP:8000`。
   查电脑 IP：按 `Win + R`，输入 `cmd` 回车，再输入 `ipconfig` 回车，
   找「无线局域网适配器 WLAN」下的 `IPv4 地址`，例如 `192.168.1.8`，手机就开 `http://192.168.1.8:8000`。

---

## 二、导入淘宝 Cookie（小白图文版）

> 没配 Cookie 时点「分析」会提示 **登录已过期/未配置**（AUTH_REQUIRED）。Cookie 就是你的淘宝登录凭证，配一次即可，过期了重新导一次。

### 方法 A：网页设置区导入（推荐）

1. 电脑 Chrome（或 Edge）打开 `taobao.com`，**登录你的淘宝账号**；
2. 按键盘 **F12**（打开开发者工具），点顶部的 **Network**（网络）标签；
3. 按 **F5 刷新页面**，Network 列表会出现一堆请求，**点第一行**；
4. 右侧找到 **Request Headers（请求标头）** → 往下找 **Cookie:** 开头的那一行；
5. 鼠标从 `Cookie:` 后面开始，**整串全选复制**（很长、分号分隔的一大段）；
6. 打开真评页面，展开底部 **⚙️ 设置 · 导入淘宝 Cookie**，粘贴进输入框，点 **保存 Cookie**；
7. 看到「✓ 已保存」，回到顶部重新点「分析」。

### 方法 B：直接改配置文件

用记事本打开项目目录下的 `config_local.py`（没有就从 `config_local.example.py` 复制一份改名），把 cookie 粘到引号里：

```python
TAOBAO_COOKIE = "把你复制的整串cookie粘到这里"
```

保存后重启 `start.bat` 生效。

### Cookie 过期了？

症状：又能访问淘宝但「分析」报 AUTH_REQUIRED。重复方法 A 导一次新 Cookie 即可（网页上保存会自动覆盖旧的）。

---

## 三、看懂报告

| 区块 | 含义 |
|---|---|
| 大字母评级 | A 很干净 → B → C → D → F 水军重灾区；U 评论太少无法判断 |
| ★4.8 → ★4.2 | 原始均分 → 只算「真实评论」的均分（差得越多水越多） |
| 三色占比条 | 绿=真实 / 黄=可疑 / 灰=边界（V0 不自动判死） |
| 疑似水军 tab | 每条附命中理由（红字），如「超短模板好评」「与同簇5条高度相似」 |

---

## 四、常见问题

| 提示 | 原因与处理 |
|---|---|
| AUTH_REQUIRED：登录已过期 | Cookie 没配或过期 → 按上面第二节重新导入 |
| BAD_URL：链接无法识别 | 只支持淘宝/天猫商品页链接，重新从商品页地址栏整串复制 |
| NO_RATE_API：没有抓到评论数据 | 商品没有评论，或淘宝改版（需要更新 `backend/fetcher.py` 顶部常量区） |
| TIMEOUT：抓取超时 | 网络波动，稍后再试一次 |
| 手机打不开页面 | 确认手机和电脑连**同一个 WiFi**；Windows 防火墙首次放行 Python（弹窗点「允许访问」）；或临时关闭「公用网络」防火墙测试 |
| 浏览器内核下载卡住 | 开启代理工具后再运行 `start.bat`（脚本会自动重试） |

---

## 五、开发者说明

```bash
# 运行测试（过滤管线单元测试，零外部依赖）
.venv\Scripts\python -m pytest tests/ -v

# 手动起服务
.venv\Scripts\python -m uvicorn backend.api:app --host 0.0.0.0 --port 8000
```

目录结构：

```
backend/
  api.py               FastAPI 入口（/api/analyze、/api/settings/cookie、/api/health、/）
  fetcher.py           Playwright 抓取（链接解析→开页→旁听评论接口→翻页收集）
  cleaner.py           清洗（切分/去 UI 残留/空白规范化/重复标记）
  filter_heuristic.py  规则过滤主体（纯函数，阈值集中在顶部常量区）
  llm_layer.py         LLM 空壳（V0 不实现，仅定义可插拔签名）
web/index.html         单文件前端（手机 Safari 优先）
tests/                 过滤管线测试 + 假评论 fixtures
config_local.py        本地凭据（已被 .gitignore 排除，绝不入库）
```

**V0 边界（不做的事）**：LLM 任何调用 / iOS 快捷指令 / 淘口令解析 / 京东与小红书 / 用户系统 / 数据库 / 公网部署 / 并发批量抓取 / 手机 App。
架构留口子：过滤输出保留 `gray` 桶；`llm_layer.py` 已定义好函数签名。
