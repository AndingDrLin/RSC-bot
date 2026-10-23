# 06 · 工程规范与开源参考

---

## 1. 技术选型

| 层 | 选择 | 版本 | 理由 | 放弃的备选 |
|---|---|---|---|---|
| 语言 | Python | 3.12 | 量化生态最全（polars/pandas/qlib/statsmodels）；瓶颈在网络与 60 s tick，不在 CPU | Rust（开发慢 3–5×，本项目吞吐不需要）、Go（量化库少） |
| 包管理 | uv | 最新 | 锁文件 + 秒级安装，Docker 层缓存友好 | poetry（慢）、pip-tools |
| HTTP | httpx（async）+ `httpx.Client(http2=True)` | 0.27+ | 原生 async、超时/重试可控、支持 cookie jar | requests（同步，阻塞事件循环） |
| 校验 | pydantic v2 | 2.x | 响应模型强类型、解析快（Rust core） | dataclass（无校验） |
| 数据处理 | polars + duckdb | 最新 | 列式 + 零拷贝，1 年 60 标的秒级 | pandas（内存/速度差 3–10×） |
| 存储 | Parquet + SQLite(WAL) | — | 单机零运维，文件即备份 | Postgres（运维成本 > 收益） |
| 缓存/队列 | Redis 7 | — | 令牌桶、跨进程缓存、分布式锁 | 内存队列（多进程不可见） |
| 调度 | asyncio + APScheduler | — | 单进程、可注入时钟 | Celery（重） |
| 日志 | structlog（JSON） | — | 结构化、trace_id 贯穿 | logging（难查询） |
| 指标 | prometheus_client | — | 与 Grafana 天然集成 | 自建 |
| 面板 | FastAPI + 静态页 / Grafana | — | 只读监控，不做交易入口 | Streamlit（交互重） |
| 测试 | pytest + respx + hypothesis + freezegun | — | HTTP 可 mock、属性测试、时钟可控 | unittest |
| 质量 | ruff + mypy(strict) + pre-commit | — | 快、统一 | flake8+isort+black（慢） |
| 容器 | Docker + Compose v2 | — | 单机即可，无需 k8s | k8s（过度设计） |

---

## 2. 可借鉴的开源项目（按"借什么"分类）

| 项目 | 语言 | 借什么 | 不借什么 |
|---|---|---|---|
| [vn.py / VeighNa](https://github.com/vnpy/vnpy) | Python | **事件引擎 + 网关抽象 + 策略模板**三层结构；国内量化事实标准 | CTP 相关、GUI 交易终端 |
| [freqtrade](https://github.com/freqtrade/freqtrade) | Python | 策略接口、hyperopt 参数搜索、回测报告格式、`protections` 概念 | 其交易所适配（面向 ccxt 现货） |
| [NautilusTrader](https://github.com/nautechsystems/nautilus_trader) | Rust+Py | **事件驱动内核 + 撮合仿真 + 确定性回放**的设计思想；性能基准 | Rust 实现（学习成本高） |
| [Qlib](https://github.com/microsoft/qlib) | Python | **因子表达式引擎**、数据集抽象、模型 zoo、滚动训练流水线 | 其重型 ML 基础设施 |
| [vectorbt](https://github.com/polakowo/vectorbt) | Python | 向量化回测的参数网格与指标计算范式 | 其"无撮合细节"的近似（我们只用于筛选） |
| [ccxt](https://github.com/ccxt/ccxt) | JS/Py | **交易所统一适配层**的接口设计（`fetch_ohlcv/create_order/cancel_order`） | 其庞大交易所矩阵 |
| [Hummingbot](https://github.com/hummingbot/hummingbot) | Python | 订单管理、仓位跟踪、连接器抽象 | 做市逻辑（本平台无挂单） |
| [QuantConnect Lean](https://github.com/QuantConnect/Lean) | C# | 回测/实盘同构（同一份算法代码）、数据订阅模型 | 其 .NET 技术栈 |
| [alphalens](https://github.com/quantopian/alphalens) | Python | 因子 IC/分层/换手分析报告 | 已停止维护，自行实现核心 |
| [Riskfolio-Lib](https://github.com/dcajasn/Riskfolio-Lib) | Python | 风险平价、CVaR、约束优化 | 重型依赖（仅参考公式） |
| [pyfolio / quantstats](https://github.com/ranaroussi/quantstats) | Python | 绩效报告指标口径（Sharpe/Calmar/回撤） | 其绘图依赖 |

**结论**：本项目 = vn.py 的**分层思想** + freqtrade 的**策略/回测流程** + nautilus 的**确定性** + qlib 的**因子工程**。不引入任何一个作为运行时依赖（定制成本高于自研），只借设计。

---

## 3. 幻方量化的公开方法论（可迁移的部分）

> 以下为公开报道/演讲中反复出现的要点（概括，非逐字引用），只取对本项目有操作意义的部分。

| 公开要点 | 本项目如何落地 |
|---|---|
| **"数据、算力、算法"三要素**，数据是根基 | M2 数据管道优先于策略；先有 1 年干净 5m 数据再谈模型 |
| **因子挖掘走向自动化/端到端**（从人工因子到深度学习） | M5 先做可解释因子（动量/反转/价差），再考虑树模型/线性组合；**不直接上深度网络**（数据量不足 1 年） |
| **算力投入是长期壁垒** | 本项目反向：算力极小，因此靠**约束更少、成本更低的市场**（FX 2bp、crypto 24h）取胜，而不是拼模型 |
| **严格的风险控制与组合优化** | M4 规则引擎 + M5 风险平价 + 波动率目标，风控独立于策略 |
| **回测与实盘一致性**是生命线 | ADR-006：策略只产意图；ADR-007：同一份代码跑 Paper/实盘 |
| **多策略、多周期分散** | 3 族策略 + 5 个市场类别，族间风险平价 |
| **工程化：研究可复现** | 实验台账（config/git_sha/data_version）+ 固定报告模板 |

**不迁移的部分**：幻方的 alpha 来源是 A 股高频量价数据 + 大规模算力 + 数百人团队；本项目是"一个人 + 608 RSC + 一个社区虚拟交易所"，直接对标其模型复杂度是错的。可迁移的只有**方法论纪律**：数据先行、成本敏感、风控独立、回测可信。

---

## 4. 编码规范

### 4.1 目录与命名
- 模块小写、下划线；类 PascalCase；常量 UPPER。
- 一个文件一个职责，> 400 行考虑拆分。
- 禁止循环导入：`exchange → marketdata → strategy → execution → risk` 单向依赖，`risk` 不依赖 `execution`。

### 4.2 类型
- `src/` 全量 `mypy --strict`；`Any` 必须带 `# type: ignore[reason]` 注释。
- 金额/数量用 `Decimal`（与交易所返回的字符串对齐），**禁止 float 参与记账**；绩效计算可用 float。
- 时间统一 `datetime`（aware，UTC）；禁止 naive datetime。

### 4.3 错误处理
- 领域异常继承 `RscError`；网络层异常必须转译，禁止裸 `except Exception` 吞掉。
- 所有重试必须幂等；下单重试必须复用 `request_id`。
- 失败必须"可观测"：日志 + 指标 + 审计三选二以上。

### 4.4 配置
- 禁止硬编码魔法数字；交易所规则进 `config/rules.yaml`。
- 每个配置项必须有默认值、类型、注释、单测。

### 4.5 提交
- Conventional Commits；PR 模板含"影响范围/测试方式/回滚方案"。
- 每次提交可独立回滚。

---

## 5. 依赖清单（预期）

```toml
[project]
requires-python = ">=3.12"
dependencies = [
  "httpx[http2]>=0.27",
  "pydantic>=2.7",
  "pydantic-settings>=2.3",
  "structlog>=24.1",
  "prometheus-client>=0.20",
  "apscheduler>=3.10",
  "redis>=5.0",
  "duckdb>=1.1",
  "polars>=1.9",
  "pyarrow>=17",
  "fastapi>=0.115",
  "uvicorn>=0.30",
  "pyyaml>=6.0",
  "tenacity>=8.5",
]
[dependency-groups]
dev = ["pytest>=8", "pytest-asyncio", "pytest-cov", "respx", "hypothesis",
       "freezegun", "ruff", "mypy", "pre-commit", "pip-audit"]
```

许可证要求：全部 MIT/BSD/Apache-2.0，无 GPL 传染风险（重点核查 `duckdb` MIT、`polars` MIT、`pyarrow` Apache-2.0）。

---

## 6. 架构决策记录（ADR 索引）

| ID | 决策 | 状态 | 日期 |
|---|---|---|---|
| ADR-001 | 单一出网网关（30 req/min 全局调度） | accepted | 2026-10-23 |
| ADR-002 | Python 3.12 而非 Rust | accepted | 2026-10-23 |
| ADR-003 | 单进程 bot-core | accepted | 2026-10-23 |
| ADR-004 | DuckDB + Parquet | accepted | 2026-10-23 |
| ADR-005 | SQLite 记录交易 | accepted | 2026-10-23 |
| ADR-006 | 策略只产意图 | accepted | 2026-10-23 |
| ADR-007 | 自建 Paper Broker（无 demo 账户） | accepted | 2026-10-23 |
| ADR-008 | YAML 配置 + 环境变量覆盖 | accepted | 2026-10-23 |

新增 ADR 用模板：`docs/adr/NNNN-title.md`（背景/选项/决策/后果）。

---

## 7. 团队技能矩阵（若只有 1 人）

| 能力 | 需求 | 若缺失的应对 |
|---|---|---|
| Python 异步编程 | 高 | 先写同步版，M2 前重构 |
| 量化/统计 | 中高 | 用 qlib/alphalens 的成熟指标，不自己发明 |
| 运维/Docker | 中 | Compose 起单机，不碰 k8s |
| 金融微观结构 | 中 | 前 2 周读交易所规则 + 实测，别猜 |

---

## 8. 代码之外的资产

比代码更值钱的三样东西，必须持续维护：

1. **规则字典** `config/rules.yaml` —— 交易所改规则时，改这里而不是改代码。
2. **实验台账** `experiments.parquet` —— 每个策略/参数的结论与证据。
3. **事故复盘** `docs/incidents/` —— 每次故障的根因与修复，防止重复踩坑。
