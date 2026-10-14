"""本地配置模板。

用法：把本文件复制为 config_local.py（同目录），填入真实值。
config_local.py 已被 .gitignore 排除，绝不入库；真实凭据不通过聊天/仓库传递。
"""

# 淘宝 Cookie 字符串：浏览器登录淘宝后，F12 → Network → 任一请求 →
# Request Headers → Cookie 整串复制粘贴到这里（详见 README 教程）。
TAOBAO_COOKIE = ""

# DeepSeek API key：V0 未使用（llm_layer.py 为空壳），为后续版本预留。
DEEPSEEK_API_KEY = ""
