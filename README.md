# LLM Quota Monitor

统一监控三家 LLM 厂商的**小时 / 周 / 月**额度消耗，集成 Grafana + Prometheus + Telegram 告警。

| Provider | 鉴权方式 | 数据维度 |
|---|---|---|
| **minimaxi.com** (MiniMax CN) | 官方 API (Bearer token) | 5h · weekly · monthly (=weekly×4.345) |
| **OpenCode Go** | Browser cookie file + workspace ID | 5h · weekly · monthly |
| **Kimi Code** | API key (`api.kimi.com/coding/v1/usages`) | 5h · weekly · monthly（=月度余额，见下）· booster（=加速包花费） |

### Kimi 的两个"月度"不是一回事 ⚠️

Kimi 有两个带 "monthly" 字样的东西，**含义完全不同**，Dashboard 上也是两个独立的窗口：

| 窗口 | 含义 | 数据来源 |
|---|---|---|
| `monthly` | **月度余额** —— Kimi console 上显示的那个数 | membership API |
| `booster` | **加速包本月花了多少钱** ÷ 本月上限 | Code API 的 `booster_wallet` |

`booster_wallet.monthlyUsed / monthlyChargeLimit` 里的 "monthly" 指的是**加速包的计费周期**，
不是月度余额配额。用 `balance.amount`（7500 分 = ¥75）和 `balance.amountLeft`（481.03 分 = ¥4.81）
一减就知道：`monthlyUsed` = 7019 分 = ¥70.19，就是这个周期里花掉的钱。

**月度余额只有 membership API 有**（`subscriptionBalance.amountUsedRatio`，就是 console 显示的数字）。
Code API 里的 `usage` / `limits` 是**请求数**池（5h=100、weekly=100），`usages.limit_*` 是这些
请求池的比例 —— 上游根本没有任何"月度余额"字段。

> 逆向确认（`https://www.kimi.com/settings/subscription` 的 bundle）：
> 页面上的 **「总使用量」就是 `subscriptionBalance.amountUsedRatio`**，和 console 的月度余额
> 是同一个数。页面把它拆成两根进度条：
> `Kimi = max(总使用量 - kimiCodeUsedRatio, 0)`，`Code = kimiCodeUsedRatio`。
> 所以「总使用量」是**两根条之和**，不是 Code 那一条。
>
> 同一个页面上的「5 小时用量」「7 天用量」来自 `ratelimitCode5h` / `ratelimitCode7d`
> （字段名是 `ratio` + `resetTime`），「加速包」卡片来自 `boosterWallets[0]`。

接口契约（从线上 proto 反查确认）：

```
proto : kimi/gateway/membership/v2/membership.proto
包名  : kimi.gateway.membership.v2
服务  : MembershipService
方法  : GetSubscriptionStats

POST https://www.kimi.com/apiv2/kimi.gateway.membership.v2.MembershipService/GetSubscriptionStats
Authorization: Bearer <web token>     body: {}
```

响应字段（proto snake_case → JSON camelCase）：

| proto | JSON | 含义 |
|---|---|---|
| `subscription_balance` | `subscriptionBalance` | 月度余额：`amountUsedRatio` / `kimiCodeUsedRatio` / `expireTime` |
| `ratelimit_5h` / `ratelimit_code_5h` | `ratelimit5h` / `ratelimitCode5h` | 5 小时窗口，`ratio` + `resetTime` |
| `ratelimit_7d` / `ratelimit_code_7d` | `ratelimit7d` / `ratelimitCode7d` | 7 天窗口 |
| `booster_wallets` | `boosterWallets` | 加速包钱包（数组） |
| `gift_balances` | `giftBalances` | 赠送额度 |

> ⚠️ 主站和 `/code/console` 用**不同的资源根目录**（`kimi-web` vs `kimi-web-seo`），
> 同名文件内容不一样 —— 逆向时一定按页面自己的根目录解析。

所以月度余额的来源优先级是：

| 优先级 | 来源 | 需要什么 |
|---|---|---|
| 1 | `MembershipService/GetSubscriptionStats` → `subscriptionBalance.amountUsedRatio` | Kimi 网页 token |
| 2 | `usages.limit_month_total.used_ratio` | 只要 API key |
| 3 | *（没有 —— 不拿别的数来凑）* | — |

拿不到月度余额时，`monthly` 窗口就是空的（指标为 -1），`booster` 窗口照常有数。
实际用了哪个来源会写进 scraper 日志：

```
kimi_code: monthly balance 75.00% (source=membership API (console))
kimi_code: booster spend CNY 70.19 of CNY 100.00 (70.19%), CNY 4.81 left in pack
```

> ⚠️ 以前这里用的是 `weekly × 4.345` 推导出来的**虚构**月度值——上游从来没有这个数字，
> 告警也因此对真实的月度上限失效。

> ⚠️ 更早还犯过一个错：把加速包的花费比例（70.19%）当成了月度余额（75%）报出来。
> 现在两者是独立的窗口，Dashboard 上也是两个面板，不会再混。

> `limit_month_code` 是月度池里属于 Code 的那一块，**不是**独立额度，不会被当成 monthly 窗口。

## 停掉某个 provider

每个 provider 都有一个独立的开关，设为 `false` 就完全停掉它——不发请求、不启动
Playwright 浏览器、不产生 Telegram 失败告警：

```bash
# .env
MINIMAXI_ENABLED=true
OPENCODE_GO_ENABLED=false     # ← opencode_go 上游改版，暂时停掉
KIMI_CODE_ENABLED=true
```

改完 `docker compose up -d --force-recreate scraper` 生效（scraper 每次 tick 都重新读配置）。
日志会写 `opencode_go: disabled via config, skipping`，dashboard 上该 provider 的
面板保持上一次的数值（这是预期行为——它是"停掉"，不是"坏了"）。

> ⚠️ 停掉的 provider 不会触发任何告警。如果它本来就坏了，你也不会再收到 Telegram 通知，
> 所以别用它来掩盖还没修的问题。

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
│   └── tests/                               # 60 个测试，<1 秒跑完
│
├── grafana/                                 # ⭐ 部署到 .17 Grafana 的素材
│   ├── dashboards/
│   │   └── llm_monitor.json                 # 34 panel dashboard（uid=llm-monitor）
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

**c) 创建 7 条 alert rules**（参考 `docs/alerts.yml`）：

`docs/alerts.yml` 是 Prometheus rule 格式；Grafana Managed Alerting 用的是不同格式（参考 `docs/alerting/full.yml` 末尾的 `groups.rules[]` 段）。

⚠️ Grafana unified alerting 的 rule 用 JSON 格式定义（data + condition 两段），通过 `POST /api/v1/provisioning/alert-rules` 上传。**一个规则 = 一个 POST 请求**，7 条规则 = 7 次调用。

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

`grafana/provisioning/alerting/rules.yaml`：参考 `docs/alerting/full.yml` 末尾的 `groups.rules[]` 段（很长，包含 7 条规则）。

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
# 预期：7
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
| grafana 渲染 dashboard | 浏览器打开 `http://192.168.3.17:3000/d/llm-monitor/llm-monitor` | 34 panel 都显示数字（非 "--"）|
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

## Cookie 抓取

⚠️ Cookie 走 cookie 鉴权，**会过期**。scraper 检测到连续失败会通过 Telegram 告警。

> Cookie 文件存放在 `cookie/` 目录下，格式为 **Netscape**（Chrome 扩展 Cookie-Editor 导出）。

### OpenCode Go

1. 打开 https://opencode.ai/workspace/<id>/go，**完成登录**
2. 用 Cookie-Editor 插件导出该站点的 cookie（Netscape 格式）到 `cookie/opencode_cookie`
3. 文件里必须包含名为 **`auth`** 的 cookie，缺少它 `is_configured()` 返回 False，scraper 自动跳过

```bash
OPENCODE_GO_COOKIE_FILE=cookie/opencode_cookie
```

### Kimi（月度额度，可选）

Kimi 的月度池百分比需要 www.kimi.com 的**登录 token**。

⚠️ **新版 Kimi 网页不再写 `kimi-auth` cookie 了**——JWT 存在浏览器的 localStorage
里（`access_token`），所以 Cookie-Editor 导出的 cookie 文件里根本没有它。
导出的 cookie 只够做访客身份，membership 接口会返回 401。

拿 token 的四种方式，任选其一：

**方式 A：扫码登录（最省事，不需要 cookie）** ⭐

```bash
python scripts/kimi_qr_login.py
```

终端会打印一个二维码（和它对应的链接），**用 Kimi App 扫一下**、在手机上确认，
脚本就会轮询到 `access_token`、先拿真实接口验一次，再写进 `cookie/kimi_token`。
全程不需要浏览器 profile、不需要 DevTools、**不需要导出 cookie**。

终端里的字符二维码手机不好扫，所以脚本会**同时写一个 PNG**：

```
cookie/kimi_token.qr.png
```

用浏览器打开这个 PNG，或者传到手机上扫，都行。想在手机上直接看，起个静态服务：

```bash
python scripts/kimi_qr_png.py --serve      # 默认 http://<host>:8899/kimi_login_qr.png
```

> ⚠️ 扫**二维码**（Kimi App），不要直接打开 `wechat/mp/auth` 那个链接 ——
> 那个页面是一张"请用微信打开"的静态海报，本身不画二维码。

> 依赖 Kimi 自己的二维码登录接口（`auth.kimi.com` 的 `AuthService`），
> 没装 `qrcode` 包也能跑（只打印链接，不出 PNG）。

**方式 B：从已登录的浏览器里取（脚本）**

```bash
# 复用已登录的 Chrome —— 不用交互，token 已经在 localStorage 里了
python scripts/fetch_kimi_token.py --profile ~/.config/google-chrome

# 或者开一个浏览器，手动登录后回车
python scripts/fetch_kimi_token.py
```

脚本会把 token 写到 `cookie/kimi_token`（权限 600），**并且会先拿真实接口
验证一次**（`GetSubscriptionStats` 返回 200 才写入），避免存一个必然 401 的
token。验证失败会明确告诉你原因，不会写文件。

> 服务器上没有图形界面时，在**你自己登录了 Kimi 的那台机器**上跑，
> 然后把 `cookie/kimi_token` 传上来即可。

**方式 C：手动**

浏览器 F12 → Application → Local Storage → `https://www.kimi.com` → 找
`access_token` → 复制整段值 → 存到 `cookie/kimi_token`（一行，不要引号）。

> 只要值那一行，**不要带引号**，也不要带 `"access_token":` 前缀。

**方式 D：直接写 .env**

```bash
KIMI_AUTH_TOKEN=eyJhbGciOi...
```

优先级：`KIMI_AUTH_TOKEN`（.env） > `cookie/kimi_token` > cookie 文件里的 `kimi-auth`。
三种方式都支持，老的 `kimi-auth` cookie 还能用。

### token 只有 15 分钟寿命 —— refresh token 才是关键

Kimi 的 access token 是**短命的**（`exp - iat == 900` 秒 = 15 分钟）。
但同一次登录返回里还有一个 `refresh_token`，有效期到 **2027-01-24**（约 3 个月）。

扫码脚本会把两个都写下来：

```
cookie/kimi_token          # access token，15 分钟
cookie/kimi_refresh_token  # refresh token，几个月
```

scraper 在 access token 过期时自动用 refresh token 换新的，并写回文件 ——
所以**登录一次就能一直用**，不用反复扫码。

如果 refresh token 也失效了（或者你没保存），重新扫码即可：

```bash
python scripts/kimi_qr_login.py
```

> ⚠️ 两个 token 都在 `cookie/` 目录里，已被 `.gitignore` 忽略、权限 600。
> refresh token 的权限和 access token 一样高，别外传。

拿到 token 之后，容器的 `cookie/` 目录是 `:ro` 挂载的，直接把
`cookie/kimi_token` 放进去即可，不用改 docker-compose：

```bash
cp kimi_token /root/workspace/llm_monitor/cookie/kimi_token
chmod 600 /root/workspace/llm_monitor/cookie/kimi_token
docker compose up -d --force-recreate scraper
```

验证是否生效，看容器日志里这一行：

```
kimi_code: monthly balance 75.00% (source=membership API (console))
```

`source=membership API (console)` 就说明用的是 console 的官方月度余额；如果日志里是
`no monthly balance available`，说明 token 还没被读到（去日志里找
`no Kimi web token found` 或 `membership API auth failed`）。
加速包那一行是独立的，跟月度余额没关系：

```
kimi_code: booster spend CNY 70.19 of CNY 100.00 (70.19%), CNY 4.81 left in pack
```

```bash
KIMI_COOKIE_FILE=cookie/kimi_cookie
KIMI_TOKEN_FILE=cookie/kimi_token
KIMI_AUTH_TOKEN=
```

token 过期时 scraper 会打日志 `the Kimi web token has expired`。provider 不会挂：
加速包和请求数池照常有数，只是**月度余额**这个窗口会空掉（指标 -1），因为它只有
console 才有。

路径是相对于项目根目录的相对路径。

### `.env` 配置

```bash
# MiniMax API key (长期有效)
MINIMAX_API_KEY=eyJhbG...*** OpenCode Go
OPENCODE_GO_WORKSPACE_ID=abc123def
OPENCODE_GO_COOKIE_FILE=cookie/opencode_cookie

# Kimi Code (Bearer token)
KIMI_API_KEY=***
KIMI_COOKIE_FILE=cookie/kimi_cookie

# Telegram
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