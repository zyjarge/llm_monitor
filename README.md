# LLM Quota Monitor

统一监控三家 LLM 厂商的**小时 / 周 / 月**额度消耗，集成 Grafana + Prometheus + Telegram 告警。

| Provider | 鉴权方式 | 数据维度 |
|---|---|---|
| **minimaxi.com** (MiniMax CN) | 官方 API (Bearer token) | 5h · weekly · monthly (=weekly×4.345) |
| **OpenCode Go** | Browser cookie file + workspace ID | 5h · weekly · monthly |
| **Kimi Code** | Browser cookie file | 5h · weekly · monthly (=weekly×4.345) |

## 架构

```
                  ┌──────────────┐
                  │  Scraper     │  (Docker, Playwright + httpx)
                  │  每 5 分钟   │
                  └──────┬───────┘
            ┌─────────────┼─────────────┐
            ▼             ▼             ▼
       SQLite 历史    Pushgateway   Telegram
            │             │             │
            │             ▼             ▼
            └───▶   Prometheus     (告警阈值 / cookie 失效)
                          │
                          ▼
                    Grafana Dashboard
```

## Cookie 文件格式

⚠️ OpenCode Go 和 Kimi Code 需要浏览器 cookie，**会过期**。Cookie 失效后 scraper 会自动通过 Telegram 警告。

> Cookie 文件存放在 `cookie/` 目录下，**每个文件是一段原始 `Cookie:` header 字符串**（例如 `name=value; name2=value2`），不是单条 cookie。

### 如何导出 cookie 文件

#### Chrome / Edge：

1. 打开对应控制台，**完成登录**
2. `F12` → `Application` → `Storage` → `Cookies` → 选目标站点
3. 在 console 输入：
   ```js
   document.cookie          // 仅 JS 可访问的 cookie (httponly 看不到)
   ```
4. 或用 DevTools **Network 面板**：刷新页面，复制请求头里的 `Cookie:` 整段
5. 整段粘到 `cookie/<name>`，**单行保存**

#### 推荐：使用 curl + chrome devtools protocol（更全）

详见 `scripts/refresh_cookie.md`（可选工具脚本）。

### 各家需要的 cookie 字段

| 文件 | 必需字段 | 控制台 |
|---|---|---|
| `cookie/opencode_cookie` | `auth=...` | https://opencode.ai/workspace/`<id>`/go |
| `cookie/kimi_cookie`     | `kimi-auth=...` | https://kimi.com/code |

### 文件路径

可在 `.env` 里覆盖默认路径：

```bash
OPENCODE_GO_COOKIE_FILE=cookie/opencode_cookie
KIMI_COOKIE_FILE=cookie/kimi_cookie
```

路径是相对于项目根目录的相对路径，绝对路径也可以。

### `.env` 配置

```bash
# MiniMax API key (长期有效)
MINIMAX_API_KEY=eyJhbG...*** OpenCode Go
OPENCODE_GO_WORKSPACE_ID=abc123def
OPENCODE_GO_COOKIE_FILE=cookie/opencode_cookie.txt

# Kimi Code
KIMI_COOKIE_FILE=cookie/kimi_cookie.txt

# Telegram
TELEGRAM_BOT_TOKEN=123456:ABC...
TELEGRAM_CHAT_ID=987654321
```

### Telegram bot 配置

1. @BotFather → `/newbot` → 拿到 token
2. 给机器人发任意消息，访问 `https://api.telegram.org/bot<TOKEN>/getUpdates` 找 `chat.id`
3. 填入 `.env`

## 启动

```bash
# 1. 准备环境文件
cp .env.example .env
# 编辑 .env，填 MINIMAX_API_KEY 和 OPENCODE_GO_WORKSPACE_ID

# 2. 准备 cookie 文件
mkdir -p cookie
# 将 cookie 字符串粘进对应 .txt 文件

# 3. 启动
docker compose up -d --build

# 4. 查看日志
docker logs -f llm-monitor-scraper
```

⚠️ `docker-compose.yml` 已经把 `./cookie` 挂载进容器（`:ro` 只读），文件会被自动读到。

### 配置 Prometheus

将以下片段添加到现有 Prometheus 服务的 `prometheus.yml`：

```yaml
scrape_configs:
  - job_name: pushgateway
    honor_labels: true
    static_configs:
      - targets: ['localhost:9091']   # 改成 pushgateway 所在 IP

rule_files:
  - /path/to/this/repo/scripts/prometheus_alerts.yml
```

### 导入 Grafana Dashboard

Grafana → + → Import → 上传 `scripts/grafana_dashboard.json` → 选 Prometheus 数据源 → Import。

## 验证

### 1. 冒烟测试

不启动容器，先验证 provider 接口契约：

```bash
uv run python scripts/smoke_test.py
```

### 2. 跑测试套件

```bash
uv run pytest scraper/tests/ -v
```

应看到 `36 passed in <1s`。

### 3. 配置探测（验证 cookie 能被正确解析）

```bash
uv run python -c "
from scraper.src.providers import minimaxi, opencode_go, kimi_code
for name, fn in [('minimaxi', minimaxi.is_configured),
                 ('opencode_go', opencode_go.is_configured),
                 ('kimi_code', kimi_code.is_configured)]:
    print(f'{name:12s} configured={fn()}')
"
```

输出预期：

```
minimaxi      configured=False        # 没填 MINIMAX_API_KEY
opencode_go   configured=True|False   # 看 cookie 文件有没有 auth cookie
kimi_code     configured=True|False   # 看 cookie 文件有没有 kimi-auth cookie
```

### 4. 手动触发一次抓取（需要容器已启动）

```bash
docker exec llm-monitor-scraper \
  uv run python -c "
import asyncio
from scraper.src.providers import minimaxi, opencode_go, kimi_code

async def go():
    for name, fn in [('minimaxi', minimaxi.fetch),
                     ('opencode_go', opencode_go.fetch),
                     ('kimi_code', kimi_code.fetch)]:
        r = await fn()
        if r.success:
            for w in r.windows:
                print(f'{name:12s} {w.window:8s} {w.percent:6.2f}%')
        else:
            print(f'{name:12s} \u2717 {r.error}')

asyncio.run(go())
"
```

### 5. 检查 Pushgateway

浏览器打开 http://localhost:9091，应看到 `llm_monitor` job + 9 个 `llm_quota_percent` 样本（3 provider × 3 window）。

## 故障排查

| 症状 | 原因 / 解决 |
|---|---|
| `cookie file ... missing or has no auth cookie` | cookie 文件缺失或过期。按本文 "Cookie 文件格式" 刷新对应 `.txt` 文件 |
| `dashboard rendered but no quota cards found` | OpenCode 改版了 DOM。打开浏览器查看新选择器，更新 `scraper/src/providers/opencode_go.py` 里的 `SELECTORS` |
| `no usage cards found at kimi.com/code` | Kimi Code 改版 DOM。同上，更新 `kimi_code.py` 里的选择器 |
| 持续 `🚨 xxx cookie/认证已失效（连续失败 N 次）` Telegram 告警 | Cookie 过期。重新从浏览器导出 cookie → 覆盖 `cookie/*.txt` → `docker compose restart scraper` |
| `no models in response` (minimaxi) | API key 无效或订阅已停用 |
| Pushgateway URL 拒连 | 检查 `docker compose ps`，确保 pushgateway 容器在；外部 Prometheus scraper 要能 reach `localhost:9091` |
| 容器内读不到 cookie | 确认 `docker-compose.yml` 有 `./cookie:/app/cookie:ro` volume 行 |

## 文件结构

```
llm_monitor/
├── docker-compose.yml          # scraper + pushgateway 两容器
├── Dockerfile                  # Playwright + uv 安装
├── pyproject.toml              # uv 项目
├── .env.example                # 配置模板（拷贝后改名 .env）
├── cookie/                     # ⭐ cookie 文件目录（git 忽略）
│   ├── opencode_cookie
│   └── kimi_cookie
├── scraper/
│   ├── src/
│   │   ├── main.py             # 调度入口，5min 一轮
│   │   ├── config.py           # pydantic-settings 配置
│   │   ├── models.py           # WindowQuota / ProviderResult 数据模型
│   │   ├── metrics.py          # Prometheus gauge 构建 + push
│   │   ├── storage.py          # SQLite 历史（30 天滚动）
│   │   ├── alerts.py           # Telegram 告警（cookie 失效 + 阈值）
│   │   └── providers/
│   │       ├── _base.py        # Playwright 浏览器 session + cookie 解析
│   │       ├── minimaxi.py     # 官方 API
│   │       ├── opencode_go.py  # 抓包（auth cookie）
│   │       └── kimi_code.py    # 抓包（kimi-auth cookie）
│   └── tests/                  # 36 个测试，<1 秒跑完
└── scripts/
    ├── smoke_test.py           # 不依赖外部服务的快速验证
    ├── grafana_dashboard.json  # 直接 import
    ├── prometheus_alerts.yml   # 告警规则
    └── prometheus_scrape.yml   # Prometheus scrape config 片段
```

## 维护

1. **每次 cookie 失效告警**：刷新对应 `cookie/*.txt` → `docker compose restart scraper`
2. **dashboard 改版**：浏览器 F12 查看新 DOM → 更新对应 provider 的 `SELECTORS`
3. **MiniMax 改额度规则**：检查 `EXPECTED_WINDOWS` + `provider` 文件里的窗口聚合逻辑
