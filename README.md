# LLM Quota Monitor

统一监控三家 LLM 厂商的**小时 / 周 / 月**额度消耗，集成 Grafana + Prometheus + Telegram 告警。

| Provider | 鉴权方式 | 数据维度 |
|---|---|---|
| **minimaxi.com** (MiniMax CN) | 官方 API (Bearer token) | 5h · weekly · monthly (=weekly×4.345) |
| **OpenCode Go** | Browser cookie file + workspace ID | 5h · weekly · monthly |
| **Kimi Code** | API key (`api.kimi.com/coding/v1/usages`) | 5h · weekly · monthly (=weekly×4.345) |

## 部署架构（双机）

```
┌──────── 192.168.3.20 ────────┐          ┌──────── 192.168.3.17 ────────────┐
│                             │          │                                │
│   docker compose up -d      │          │   已有容器（5 个月前起的）：    │
│                             │          │     - prometheus  (:9090)      │
│   ┌──────────────┐          │          │     - grafana     (:3000)      │
│   │   scraper    │  push    │          │     - node_exporter (:9100)   │
│   │  (5min tick) │──────┐   │          │                                │
│   └──────┬───────┘      │   │          │   ⭐ 直接接入，不另起新容器     │
│          ▼              │   │          │                                │
│   ┌──────────────┐      │   │  LAN     │   ┌──────────────────────┐     │
│   │ pushgateway  │      │   │ scrape   │   │  prometheus.yml      │     │
│   │  (:9091)     │◀─────┼───┼──────────┼──▶│  + pushgateway job   │     │
│   └──────────────┘      │   │          │   └──────────────────────┘     │
│           SQLite 历史    │   │          │           │                   │
│           (./data)       │   │          │           ▼                   │
│                          │   │          │   ┌──────────────────────┐     │
│                          │   │          │   │  grafana             │     │
│                          │   │          │   │  (:3000)             │     │
│                          │   │          │   │   + datasource       │     │
│                          │   │          │   │   + dashboard       │     │
│                          │   │          │   │   + alerting rules  │     │
│                          │   │          │   └──────────────────────┘     │
│                          │   │          │           │                   │
└──────────────────────────┘   │          │           ▼  Telegram webhook
                               │          │   📲 阈值告警 / cookie 失效
                               │          └────────────────────────────────┘
```

**关键事实**：
- **scraper + pushgateway 在 .20**：因为 scraper 需要 Playwright Chromium（编译期大），且有 SQLite 历史（避免 NFS 风险）
- **Prometheus + Grafana 在 .17（已有）**：纯展示/告警，无副作用
- 两者通过 LAN scrape（`192.168.3.0/24`）连接，`.17` 上的 Prometheus 每 15s 抓 `.20:9091` 的 pushgateway

---

## 项目结构

```
llm_monitor/
├── docker-compose.yml                       # .20 上的 scraper + pushgateway 两容器
├── Dockerfile                               # Playwright + uv 安装
├── pyproject.toml                           # uv 项目
├── .env.example                             # 配置模板（拷贝后改名 .env）
├── cookie/                                  # ⭐ cookie 文件目录（git 忽略）
│   ├── opencode_cookie                      # OpenCode Go 浏览器 cookie
│   └── kimi_cookie                          # 已弃用：kimi 改用 API key
│
├── scraper/
│   ├── src/
│   │   ├── main.py                          # 调度入口，5min 一轮
│   │   ├── config.py                        # pydantic-settings 配置
│   │   ├── models.py                        # WindowQuota / ProviderResult 数据模型
│   │   ├── metrics.py                       # Prometheus gauge 构建 + push
│   │   ├── storage.py                       # SQLite 历史（30 天滚动）
│   │   ├── alerts.py                        # Telegram 告警（cookie 失效 + 阈值）
│   │   └── providers/
│   │       ├── _base.py                     # Playwright 浏览器 session + cookie 解析
│   │       ├── minimaxi.py                  # 官方 API（Bearer token）
│   │       ├── opencode_go.py               # 抓包（auth cookie + workspace ID）
│   │       └── kimi_code.py                 # Kimi Code Platform API（Bearer token）
│   └── tests/                               # 36 个测试，<1 秒跑完
│
├── grafana/                                 # ⭐ 部署到 .17 Grafana 的素材
│   ├── dashboards/
│   │   └── llm_monitor.json                 # 31 panel dashboard（uid=llm-monitor）
│   └── provisioning/
│       ├── datasources/
│       │   └── prometheus.yml               # 默认 datasource → localhost:9090
│       └── dashboards/
│           └── provider.yml                 # dashboard provider → /var/lib/grafana/dashboards
│
├── prometheus/
│   └── prometheus.yml                       # .17 上的 prometheus 配置（加 pushgateway job）
│
├── docs/
│   ├── alerts.yml                           # Prometheus rule 格式（6 rules）参考
│   └── alerting/
│       └── full.yml                         # Grafana Unified Alerting provisioning（3 in 1）
│
└── scripts/
    ├── smoke_test.py                        # 不依赖外部服务的快速验证
    └── prometheus_scrape.yml                # Prometheus scrape config 片段（备用）
```

---

## 第一步：在 .20 上启动 scraper + pushgateway

```bash
cd ~/workspace/llm_monitor

# 1. 准备环境文件
cp .env.example .env
# 编辑 .env，填入：
#   - MINIMAX_API_KEY=*** OpenCode Go (auth cookie + workspace ID)
#   - KIMI_API_KEY=*** Telegram bot token + chat_id

# 2. 准备 cookie 文件
mkdir -p cookie
# 把 OpenCode Go 的 auth cookie 整段粘到 cookie/opencode_cookie

# 3. 启动
docker compose up -d --build

# 4. 验证
docker logs -f llm-monitor-scraper
# 应看到 "scrape tick done: 3/3 providers ok"

# 5. 验证 pushgateway 收到 metric
curl -s http://localhost:9091/metrics | grep llm_quota_percent
```

---

## 第二步：在 .17 上接入 Prometheus

### 2.1 修改 .17 上的 prometheus.yml

`.17` 上的 prometheus 容器配置文件位于 **`/opt/prometheus/prometheus.yml`**（宿主机 bind mount）。

**a) 备份当前配置**：

```bash
ssh root@192.168.3.17 'cp /opt/prometheus/prometheus.yml /opt/prometheus/prometheus.yml.bak-$(date +%Y%m%d)'
```

**b) 在 `scrape_configs:` 段末尾追加 pushgateway job**（在最后一个 job 的 `labels:` 之后添加）：

```yaml
  # ─── llm_monitor scraper pushgateway on 192.168.3.20 ───────────
  # scraper pushes metrics to pushgateway:9091 every 5 minutes.
  # honor_labels: true keeps the scraper's `job` label (otherwise
  # prometheus would overwrite it with the scrape job name).
  - job_name: "pushgateway"
    honor_labels: true
    static_configs:
      - targets: ["192.168.3.20:9091"]
        labels:
          source_host: "192.168.3.20"
```

⚠️ **不要同时改 `rule_files:`** —— `.17` 上的 prometheus 没加 `--web.enable-lifecycle`，配置只能 `docker restart` 重载。如果只在 `prometheus.yml` 改 scrape_configs，restart 即可。

**c) 重启 prometheus 容器**：

```bash
ssh root@192.168.3.17 'docker restart prometheus'
```

**d) 验证 pushgateway 被识别为 `up`**：

```bash
curl -s http://192.168.3.17:9090/api/v1/targets | python3 -c "
import json, sys
d = json.load(sys.stdin)
for t in d['data']['activeTargets']:
    err = (t.get('lastError') or 'ok')[:80]
    print(f\"  {t['labels']['job']:<14} {t['health']:<10} {err}\")
"
```

预期：3 个 `up`（node / prometheus / **pushgateway**）。

---

## 第三步：在 .17 上部署 Grafana Dashboard

我们使用 **Grafana Dashboard API** 直接覆盖（`uid=llm-monitor`），无需在 UI 操作。

### 3.1 一次性操作（首次部署）

```bash
# 0. 把仓库目录推到 .17（任选一种）
#    选项 A：sshpass + scp
#    选项 B：rsync
#    选项 C：git clone
#    本 README 假设你用 sshpass：
sshpass -p 'root' rsync -av --delete \
    /root/workspace/llm_monitor/ \
    root@192.168.3.17:/root/workspace/llm_monitor/

# 1. 在 .20 上确认 dashboard JSON 存在
ls -la ~/workspace/llm_monitor/grafana/dashboards/llm_monitor.json

# 2. 通过 Grafana API 导入 dashboard
#    uid 锁定为 'llm-monitor'，这样后续覆盖式更新用同一个 uid 即可
DASHBOARD_FILE=/root/workspace/llm_monitor/grafana/dashboards/llm_monitor.json

curl -s -X POST -u admin:admin \
     -H 'Content-Type: application/json' \
     --data-binary @<(jq '{
         dashboard: .,
         overwrite: true,
         message: "init dashboard deploy"
       } | .dashboard.datasource = {type:"prometheus", uid:"PBFA97CFB590B2093"}
                | .dashboard.id = null
                | .dashboard.uid = "llm-monitor"' $DASHBOARD_FILE) \
     http://192.168.3.17:3000/api/dashboards/db
```

⚠️ **`uid` 必须是 `PBFA97CFB590B2093`** —— 这是 .17 上默认 datasource "Prometheus"（指向 `http://localhost:9090`）的 uid。在 dashboard JSON 里把所有 panel 的 `datasource` 字段改成这个 uid 形式（`{type, uid}`），否则 panel 会显示 "datasource not found"。

### 3.2 后续更新（dashboard JSON 改了之后）

```bash
# 同样的覆盖 API，message 写新版本说明
curl -s -X POST -u admin:admin \
     -H 'Content-Type: application/json' \
     --data-binary @<(jq '{
         dashboard: .,
         overwrite: true,
         message: "v2: split trend into 3 panels"
       } | .dashboard.datasource = {type:"prometheus", uid:"PBFA97CFB590B2093"}' \
        /root/workspace/llm_monitor/grafana/dashboards/llm_monitor.json) \
     http://192.168.3.17:3000/api/dashboards/db
```

响应包含 `"id": 7, "version": N` —— `version` 自增代表成功。

### 3.3 验证 dashboard 已生效

```bash
# 列 dashboard（应包含 uid=llm-monitor）
curl -s -u admin:admin 'http://192.168.3.17:3000/api/search?query=LLM&type=dash-db' | jq

# 拉 panel 数量（应是 31）
curl -s -u admin:admin 'http://192.168.3.17:3000/api/dashboards/uid/llm-monitor' \
  | jq '.dashboard.panels | length'
```

浏览器打开：`http://192.168.3.17:3000/d/llm-monitor/llm-monitor` （admin/admin）

---

## 第四步：在 .17 上部署 Grafana Alerting

有 **两种方式**部署 alert rules，**推荐第一种**（更现代、UI 友好）。

### 方式 A：Grafana Managed Alerting（推荐）

**a) 创建 contact point**（Telegram）：

```bash
# 替换 __TELEGRAM_BOT_TOKEN__ 和 __TELEGRAM_CHAT_ID__
curl -s -X POST -u admin:admin \
     -H 'Content-Type: application/json' \
     -d '{
       "name": "telegram-llm",
       "type": "telegram",
       "settings": {
         "token": "__TELEGRAM_BOT_TOKEN__",
         "chatid": "__TELEGRAM_CHAT_ID__",
         "parse_mode": "HTML"
       }
     }' \
     http://192.168.3.17:3000/api/v1/provisioning/contact-points
```

⚠️ `token` 和 `chatid` 写到 API 请求里，**不会**进 git（commit 时不要提交 raw token）。建议从 `.env` 读取后替换。

**b) 创建 root policy**（所有 alert 路由到 telegram-llm）：

```bash
curl -s -X PUT -u admin:admin \
     -H 'Content-Type: application/json' \
     -d '{
       "receiver": "telegram-llm",
       "group_by": ["grafana_folder", "alertname", "severity"],
       "group_wait": "30s",
       "group_interval": "5m",
       "repeat_interval": "1h"
     }' \
     http://192.168.3.17:3000/api/v1/provisioning/policies
```

**c) 创建 6 条 alert rules**（参考 `docs/alerts.yml`）：

`docs/alerts.yml` 是 Prometheus rule 格式；Grafana Managed Alerting 用的是不同格式（参考 `docs/alerting/full.yml` 末尾的 `groups.rules[]` 段）。

⚠️ Grafana unified alerting 的 rule 用 JSON 格式定义（data + condition 两段），通过 `POST /api/v1/provisioning/alert-rules` 上传。**一个规则 = 一个 POST 请求**，6 条规则 = 6 次调用。

或者**更简单**：用 `docs/alerting/full.yml` 一次性 provisioning（见下方方式 B）。

### 方式 B：Provisioning 文件（更省事）

把 3 个 YAML 文件丢进 `.17` grafana 的 provisioning 目录，**容器**自动加载。

**a) 在 .20 上准备 3 个文件**：

`grafana/provisioning/alerting/contact-points.yaml`：

```yaml
apiVersion: 1

contactPoints:
  - orgId: 1
    name: telegram-llm
    receivers:
      - uid: telegram-llm-receiver
        type: telegram
        settings:
          token: __TELEGRAM_BOT_TOKEN__      # ⚠️ 替换真 token，不要 commit
          chatid: __TELEGRAM_CHAT_ID__
          parse_mode: HTML
```

`grafana/provisioning/alerting/policies.yaml`：

```yaml
apiVersion: 1

policies:
  - orgId: 1
    receiver: telegram-llm
    group_by:
      - grafana_folder
      - alertname
      - severity
    group_wait: 30s
    group_interval: 5m
    repeat_interval: 1h
```

`grafana/provisioning/alerting/rules.yaml`：参考 `docs/alerting/full.yml` 末尾的 `groups.rules[]` 段（很长，包含 6 条规则）。

**b) 把 3 个文件 scp 到 .17 grafana 容器内**：

```bash
GRAFANA_CONTAINER=...

# 把 provisioning/alerting 整个目录拷贝进容器
docker cp ~/workspace/llm_monitor/grafana/provisioning/alerting \
     $GRAFANA_CONTAINER:/etc/grafana/provisioning/alerting

# 注意：.17 grafana 容器用 host 网络 + bind mount
# 如果你的 grafana 容器没 mount /etc/grafana/provisioning，那上面 cp 进去的
# 文件会在容器重启时丢失。生产部署建议：
#   - 把 .20 的 grafana/provisioning/ 整个目录 mount 进容器
#   - 或把文件 scp 到 .17 宿主机对应目录（如果容器 mount 了宿主机路径）
```

**c) 触发 reload**：

```bash
curl -s -X POST -u admin:admin \
     http://192.168.3.17:3000/api/admin/provisioning/alerting/reload
```

**d) 验证 alert rules 已加载**：

```bash
curl -s -u admin:admin \
     'http://192.168.3.17:3000/api/v1/provisioning/alert-rules' \
     | jq '.[] | length'
# 预期：6
```

### 4.2 验证告警能 fire（可选）

把某个 provider 的 metric 临时改成 ≥90%（trigger critical），等 10 分钟看 telegram 收不收：

```bash
# 在 .20 容器里手动 push 一个超阈值的 metric（5min 后会过期）
docker exec llm-monitor-scraper uv run python -c "
from prometheus_client import CollectorRegistry, Gauge, push_to_gateway
r = CollectorRegistry()
g = Gauge('llm_quota_percent', '', ['provider','window'], registry=r)
g.labels(provider='minimaxi', window='5h').set(95)
push_to_gateway('pushgateway:9091', job='llm_monitor', registry=r)
"
```

等 10 分钟 → telegram 应收到 `Minimaxi5hCritical` 告警。然后等 5 分钟（推送间隔），metric 自动消失，alert 转 `resolved`。

---

## 验证清单（端到端）

| 检查 | 命令 | 预期 |
|---|---|---|
| scraper 跑通 | `docker logs llm-monitor-scraper --tail 5` | `scrape tick done: 3/3 providers ok` |
| pushgateway 有 metric | `curl -s http://localhost:9091/metrics \| grep llm_quota_percent` | 9 个 `llm_quota_percent{provider=...,window=...}` 样本 |
| prometheus 抓 pushgateway | `curl -s http://192.168.3.17:9090/api/v1/targets \| jq ...` | `pushgateway: up` |
| grafana 渲染 dashboard | 浏览器打开 `http://192.168.3.17:3000/d/llm-monitor/llm-monitor` | 31 panel 都显示数字（非 "--"）|
| alerting rules 已加载 | `curl -s -u admin:admin 'http://192.168.3.17:3000/api/v1/provisioning/alert-rules' \| jq '.[] \| length'` | `6` |
| 模拟告警能 fire | 见 4.2 节 | telegram 收到 `Minimaxi5hCritical` |

---

## 故障排查

| 症状 | 原因 / 解决 |
|---|---|
| `scrape tick done: 0/3 providers ok` | scraper 配错。先看 `docker logs llm-monitor-scraper` 是哪个 provider 失败：cookie 过期 / API key 无效 / workspace ID 错 |
| prometheus `pushgateway` target `down` | `.20:9091` 不可达。从 `.17` 测：`curl http://192.168.3.20:9091/-/healthy` 必须返回 200 |
| grafana dashboard panel 显示 `Datasource not found` | dashboard JSON 里的 `datasource.uid` 跟 .17 上 datasource uid 不匹配。查 `.17` 的：`curl -s -u admin:admin http://192.168.3.17:3000/api/datasources`，替换 dashboard JSON 里的 uid |
| grafana `Alert rules: 0` | `rules.yaml` 没被 provisioning 加载。检查容器内 `/etc/grafana/provisioning/alerting/rules.yaml` 是否存在 + `reload` API 是否触发 |
| telegram 收不到 alert | `telegram-llm` contact point 没建。看 `curl -s -u admin:admin http://192.168.3.17:3000/api/v1/provisioning/contact-points`。或 alert 触发但 `repeat_interval` 已过（用 5.4 节测试 fire）|
| `Lifecycle API is not enabled` | `.17` 上的 prometheus 没加 `--web.enable-lifecycle`。配置改动只能 `docker restart`，不能 API reload |

---

## Cookie 抓取（OpenCode Go）

⚠️ OpenCode Go 走 cookie 鉴权，**会过期**。scraper 检测到连续失败会通过 Telegram 告警。

> Cookie 文件存放在 `cookie/` 目录下，**每个文件是一段原始 `Cookie:` header 字符串**（例如 `name=value; name2=value2`），不是单条 cookie。

### 如何导出 cookie

1. 打开 https://opencode.ai/workspace/<id>/go，**完成登录**
2. `F12` → `Application` → `Storage` → `Cookies` → 选 `https://opencode.ai`
3. 找名为 **`auth`** 的 cookie，复制 **Value**（只要值，不要整行）
4. 整段粘到 `cookie/opencode_cookie`，**单行保存**

### 文件路径

可在 `.env` 里覆盖默认路径：

```bash
OPENCODE_GO_COOKIE_FILE=cookie/opencode_cookie
```

路径是相对于项目根目录的相对路径。

### `.env` 配置

```bash
# MiniMax API key (长期有效)
MINIMAX_API_KEY=eyJhbG...*** OpenCode Go
OPENCODE_GO_WORKSPACE_ID=abc123def
OPENCODE_GO_COOKIE_FILE=cookie/opencode_cookie

# Kimi Code (Bearer token)
KIMI_API_KEY=*** Telegram
TELEGRAM_BOT_TOKEN=123456:ABC...
TELEGRAM_CHAT_ID=987654321
```

---

## 维护

1. **每次 cookie 失效告警**：刷新 `cookie/opencode_cookie` → `docker compose restart scraper`
2. **dashboard 改版**：浏览器 F12 查看新 DOM → 改对应 provider 的 `SELECTORS`（scraper 侧），或更新 dashboard JSON 用 API 覆盖（grafana 侧）
3. **MiniMax 改额度规则**：检查 `EXPECTED_WINDOWS` + provider 文件里的窗口聚合逻辑
4. **Alerting 阈值调整**：直接编辑 `.17` 上的 `policies.yaml` + `rules.yaml` + reload API，或 Grafana UI 里改
5. **Cookie 重新抓取（自动化）**：见 `scripts/refresh_cookie.md`（可选工具）