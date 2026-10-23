# 07 · 部署、运维与安全

---

## 1. 运行环境

| 项 | 选择 | 说明 |
|---|---|---|
| 宿主机 | 1 台 2C4G VPS | **优先欧洲（法兰克福/伦敦）**：交易所服务端在 LHR，欧洲 RTT 10–30 ms；国内直连 200–300 ms 且抖动大 |
| OS | Ubuntu 24.04 LTS | Docker 官方支持 |
| Docker | 27+，Compose v2 | |
| 时区 | 容器内 **UTC**，展示层 Asia/Shanghai | 交易所时间戳全 UTC |
| 磁盘 | 40 GB SSD | 行情 Parquet 约 2 GB/月，保留 30 天 |
| 网络 | 出站 443 放行；**不开放任何入站端口** | 面板只绑 127.0.0.1，通过 SSH 隧道访问 |

> 部署位置对"高频"影响有限（服务端 `x-runtime` 0.86 s 是主因），但对**稳定性**影响很大：国内跨境链路抖动会导致超时重试，进而浪费限频额度。

---

## 2. Docker 拓扑

```yaml
# docker/compose.yaml（骨架）
name: rscbot
x-common: &common
  image: rscbot:${TAG:-latest}
  restart: unless-stopped
  env_file: [../.env]
  logging:
    driver: json-file
    options: { max-size: "20m", max-file: "5" }
  security_opt: ["no-new-privileges:true"]
  read_only: true
  tmpfs: ["/tmp"]

services:
  gateway:
    <<: *common
    command: ["python","-m","rscbot.cli","gateway"]
    volumes: ["sessions:/data/session"]
    deploy: { resources: { limits: { cpus: "0.3", memory: 256M } } }
    healthcheck: { test: ["CMD","python","-m","rscbot.cli","health","gateway"], interval: 30s, retries: 3 }
    depends_on: [redis]

  core:
    <<: *common
    command: ["python","-m","rscbot.cli","run","--config","/config/strategy.yaml"]
    volumes:
      - ../config:/config:ro
      - state:/data/state        # SQLite
      - ../data:/data/market     # Parquet
    deploy: { resources: { limits: { cpus: "0.5", memory: 512M } } }
    depends_on: [gateway, redis]

  collector:
    <<: *common
    command: ["python","-m","rscbot.cli","collect"]
    volumes: ["../config:/config:ro", "../data:/data/market"]
    deploy: { resources: { limits: { cpus: "0.3", memory: 512M } } }

  dashboard:
    <<: *common
    command: ["uvicorn","rscbot.dashboard:app","--host","127.0.0.1","--port","8080"]
    ports: ["127.0.0.1:8080:8080"]
    volumes: ["state:/data/state:ro", "../data:/data/market:ro"]

  redis:
    image: redis:7-alpine
    command: ["redis-server","--save","60","1","--maxmemory","128mb","--maxmemory-policy","allkeys-lru"]
    volumes: ["redis:/data"]
    restart: unless-stopped

  prometheus:
    image: prom/prometheus:v2.54.1
    volumes: ["../ops/prometheus:/etc/prometheus:ro"]
    ports: ["127.0.0.1:9090:9090"]

  grafana:
    image: grafana/grafana:11.2.0
    volumes: ["grafana:/var/lib/grafana","../ops/grafana:/etc/grafana/provisioning:ro"]
    ports: ["127.0.0.1:3000:3000"]

volumes: { sessions: {}, state: {}, redis: {}, grafana: {} }
```

**Dockerfile 要点**：

```dockerfile
FROM python:3.12-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
RUN useradd -u 10001 -m app
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN pip install uv && uv sync --frozen --no-dev
COPY src/ ./src/
USER app
ENTRYPOINT ["python","-m","rscbot.cli"]
```

- 多阶段 + `uv sync --frozen`：构建可复现。
- 非 root、`read_only` 根文件系统、`no-new-privileges`：容器逃逸风险最小化。
- 镜像目标 < 400 MB。

---

## 3. 配置与密钥

`.env`（**不入库**，权限 600）：

```ini
RSC_BASE_URL=https://river-side.cc
RSC_EMAIL=linyujia@std.uestc.edu.cn
RSC_PASSWORD=__CHANGE_ME__
RSC_TIMEOUT_S=15
RSC_MAX_RPM=30
REDIS_URL=redis://redis:6379/0
LOG_LEVEL=INFO
LIVE_ENABLED=false          # 总开关，默认关
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
```

规则：
1. **密钥只走环境变量**，代码里 `pydantic.SecretStr` 包裹，`repr` 自动掩码。
2. `.env` 位于宿主机，挂载进容器仅 `core`/`gateway` 需要。
3. 禁止把密码写进 Dockerfile、镜像、日志、错误堆栈、Sentry。
4. 轮换流程：改 `.env` → `docker compose up -d` → 验证 `rscbot doctor`。

---

## 4. 监控与告警

### 4.1 指标（Prometheus）

| 指标 | 类型 | 标签 |
|---|---|---|
| `rsc_requests_total` | counter | endpoint, status, class |
| `rsc_request_seconds` | histogram | endpoint |
| `rsc_tokens_remaining` | gauge | class |
| `rsc_budget_starved_total` | counter | class |
| `orders_total` | counter | symbol, side, status |
| `order_rejects_total` | counter | rule_id |
| `order_submit_seconds` | histogram | — |
| `portfolio_equity` | gauge | — |
| `position_age_seconds` | gauge | symbol |
| `reconcile_diff` | gauge | kind(balance/position) |
| `data_gap_ratio` | gauge | exchange |
| `circuit_open` | gauge | reason |
| `tick_duration_seconds` | histogram | — |

### 4.2 告警规则（节选）

```yaml
groups:
- name: rscbot
  rules:
  - alert: ReconcileMismatch
    expr: rsc_reconcile_diff != 0
    for: 1m
    labels: { severity: P0 }
    annotations: { summary: "账本与交易所不一致，立即停止交易" }

  - alert: OrderRejectSpike
    expr: increase(orders_total{status="rejected"}[10m]) > 3
    labels: { severity: P1 }

  - alert: SessionExpired
    expr: increase(rsc_requests_total{status="302"}[5m]) > 2
    labels: { severity: P1 }

  - alert: BudgetStarved
    expr: increase(rsc_budget_starved_total[15m]) > 20
    labels: { severity: P2 }

  - alert: DataStale
    expr: rsc_quote_age_seconds > 300
    for: 5m
    labels: { severity: P2 }

  - alert: CircuitBreakerOpen
    expr: circuit_open == 1
    labels: { severity: P1 }

  - alert: ProcessDown
    expr: up{job="rscbot"} == 0
    for: 2m
    labels: { severity: P0 }
```

### 4.3 面板（Grafana）

1. **总览**：权益曲线、当日 PnL、持仓数、熔断状态、限频余量。
2. **执行**：订单漏斗（意图→下单→成交/拒单）、滑点分布、延迟分位。
3. **数据**：各交易所 tick 新鲜度、缺口率、延迟分布。
4. **系统**：CPU/内存/磁盘、请求 QPS、错误率。

---

## 5. SLO / SLI

| SLI | SLO | 测量 |
|---|---|---|
| 每日可交易时长 | ≥ 99.5% | 心跳 |
| tick 延迟 P95 | < 5 s | 直方图 |
| 下单延迟 P95 | < 2 s | 直方图 |
| 对账一致率 | 100% | 每日 |
| 数据缺口率 | < 2% | 每日 |
| P0 告警响应 | < 15 min | 值班记录 |

---

## 6. 备份与恢复

| 对象 | 频率 | 方式 | 保留 |
|---|---|---|---|
| SQLite（订单/审计） | 每小时 | `sqlite3 .backup` + 压缩 | 7 天 |
| 行情 Parquet | 每日 | 增量同步到对象存储（或本地第二块盘） | 30 天 |
| 配置/`.env` | 变更时 | 加密后离线保存（**不要**放仓库） | 永久 |
| Grafana/Prometheus | 每周 | volume 快照 | 4 周 |

**恢复演练**（M7 必做）：新机器 → 拉镜像 → 恢复备份 → `rscbot doctor` → 对账 → 允许交易，全程计时。

---

## 7. Runbook（6 类事故）

### R1 会话失效 / 登录失败
```
1. rscbot doctor --auth        # 看是否 302/400
2. 确认邮箱登录（不是用户名）
3. 若 Cloudflare 拦截：换 UA、降频、等 5 分钟
4. 恢复后确认：state 拉取成功 + 无重复订单
```

### R2 限频超限（429 / budget_starved 激增）
```
1. 立即降级：只保留 P0/P1 请求（开仓暂停）
2. 检查是否有服务绕过 gateway 直连
3. 检查重试风暴（同一请求重复入队）
4. 恢复正常后逐步放开
```

### R3 持仓/余额不一致
```
1. 触发即熔断：停止开仓
2. 拉全量 state 对比本地 SQLite
3. 以交易所为准修正本地账本，记录差异来源
4. 差异 > 1 RSC 或方向不一致 → 人工确认后再交易
```

### R4 熔断触发
```
1. 确认原因：连续亏损 / 日亏超限 / 异常波动 / 数据断流
2. 不自动复位（需人工 `rscbot circuit reset`）
3. 复盘写入 docs/incidents/
```

### R5 数据断流 / 报价过期
```
1. 检查 collector 与 gateway 日志
2. 检查交易所是否维护（state.market_data_enabled）
3. 行情恢复后先回补，再恢复策略
```

### R6 磁盘满 / 数据库损坏
```
1. 停止 core（保护状态）
2. 清理过期 Parquet（按保留策略）
3. SQLite 损坏：用最近备份恢复，重放当日订单
4. 恢复后必须对账
```

---

## 8. 安全与合规

### 8.1 账号安全
- 密码**已在需求对话中明文出现**，上线前必须**修改密码**，并更新 `.env`。
- 建议开启论坛 2FA（若支持）；至少保证邮箱 2FA。
- 会话 cookie 保存在 volume，权限 600，不写日志。
- 单账号单实例：**禁止多账号刷分、禁止绕过限频**（违反社区规则，也会被封）。

### 8.2 平台规则合规（再次强调）
- 严格遵守 30 次/分钟；客户端令牌桶是硬约束，不是建议。
- 不调用与交易无关的接口（转帐、红包、打赏）——这些有每日额度且与交易无关。
- 不尝试逆向/绕过成交确认机制；不做"报价未更新前抢跑"。
- 不刷每日奖励（`daily_reward` 由行为产生，不人为制造灌水帖）。

### 8.3 数据与隐私
- 只采集公开行情与自己账户的状态；不抓取其他用户持仓做自动化决策（虽然接口开放，但属于灰色地带，**策略不依赖**）。
- 日志中不记录密码、cookie、CSRF token。
- 若公开仓库，`data/`、`.env`、`sessions/` 全部 gitignore。

### 8.4 风险声明
- 本系统交易的是**虚拟积分**，不具备任何金融价值；但系统设计按真实交易标准执行，以便未来迁移。
- 不构成投资建议；社区成员不应据此跟单。
- 交易所可能随时改规则/关停；系统必须能"一键停止"（`LIVE_ENABLED=false` + `docker compose down`）。

---

## 9. 上线检查清单（打印出来逐项打勾）

- [ ] 密码已轮换，`.env` 权限 600，gitleaks 0 命中
- [ ] `LIVE_ENABLED=false` 默认，开启需二次确认
- [ ] 熔断阈值人工复核
- [ ] 备份任务已跑通一次并成功恢复
- [ ] 告警能送达（Telegram/邮件实测）
- [ ] Runbook 6 类事故各演练 1 次
- [ ] 只读面板可从 SSH 隧道访问
- [ ] 服务器时间同步（chrony/ntp）已启用
- [ ] 磁盘告警阈值 80%
- [ ] 有明确的"停止交易"操作人
