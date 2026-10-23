# 01 · 交易所接口与规则契约（实测版）

本文所有结论来自 2026-10-23 对 `https://river-side.cc` 的实测（登录账号：安定精神科主任），以及论坛规则帖 [topic 28918](https://river-side.cc/t/28918)。

> 契约冻结日期：2026-10-23。交易所是社区自建插件，**随时可能改版**；因此 M0 必须把本文变成可执行的契约测试（见 [04](04-test-and-acceptance.md)），任何字段变化由 CI 报警而不是由实盘亏损发现。

---

## 1. 平台本质

- Discourse 2026.10.0 + 自建插件 `discourse-rsc`（前端 bundle `rsc.js`，约 220 KB）。
- 所有交易接口都是 `https://river-side.cc/rsc/*.json`，返回 JSON，需要登录会话。
- 服务器在英国（Cloudflare `CF-RAY ...-LHR`），服务端渲染耗时（`x-runtime`）约 0.86 s，是延迟主因，不是网络。
- 匿名访问 `/rsc/*` 会 302 到 `/login`，所以**没有公开只读行情**，所有数据都要带会话拉。

---

## 2. 认证与会话

### 2.1 登录流程（已验证）

```
GET  /session/csrf.json                       -> {"csrf":"<token>"}
POST /session
     Content-Type: application/x-www-form-urlencoded
     X-CSRF-Token: <token>  X-Requested-With: XMLHttpRequest  Accept: application/json
     login=<email>&password=<password>
   -> 200 + Set-Cookie: _t=<30天>; _forum_session=<session>
```

**坑位（已踩）：**

| 现象 | 原因 | 处理 |
|---|---|---|
| `400 Bad Request`，无错误信息 | `login` 传中文用户名 | **必须用邮箱登录**（`linyujia@std.uestc.edu.cn` 或 gmail 均可） |
| `200 {"error":"用户名、电子邮件地址或密码不正确"}` | 密码错误 | 注意 200 也可能是失败，必须解析 body 判 `error` 字段 |
| 登录后 `/rsc/*` 仍 302 | 丢了 `_t` cookie | cookie jar 必须持久化；容器内挂 volume |

会话有效期约 30 天，但 Cloudflare 可能提前失效 → **必须实现 401/302 自动重登 + 重放当前请求**。

### 2.2 请求头基线

```
Cookie: _t=...; _forum_session=...
X-Requested-With: XMLHttpRequest
Accept: application/json
User-Agent: <桌面 Chrome UA>
```

`X-Requested-With` 缺失时 Discourse 会返回 HTML 而非 JSON。

---

## 3. 接口清单

### 3.1 状态快照

| 接口 | 体积 | TTFB | 说明 |
|---|---|---|---|
| `GET /rsc/state.json` | **3.39 MB** | 4.4–4.9 s | 全量：1638 个标的 + 持仓 + 订单 + 钱包 + 赛事 |
| `GET /rsc/state.json?section=market` | 98 KB | 1.2 s | **27 个热门标的**（catalog 榜）+ 持仓 + 订单 + 钱包 |
| `GET /rsc/state.json?section=wallet` | 4.5 KB | 0.93 s | 钱包/流水，**不含** instruments/positions/orders |
| `GET /rsc/state.json?section=sports` | — | — | 赛事预测 |
| `GET /rsc/state.json?section=packet` | — | — | 红包 |

> 全量 3.4 MB / 4.4 s，**禁止高频轮询**。`focus_instrument_id` 参数不影响体积（实测 3.39 MB 不变）。
> 设计结论：热门池用 `section=market`，自选池用逐标的 `history.json`，冷门标的懒加载。

### 3.2 行情历史

`GET /rsc/instruments/{id}/history.json?range=1d|5d|1y`

| range | 根数 | 粒度 | 体积 | 备注 |
|---|---|---|---|---|
| `1d` | 288 | 5 分钟 | 35.8 KB | 0.93 s |
| `5d` | 120 | 1 小时 | 15 KB | |
| `1m` / `3m` | — | — | 56 B | **422 不支持** |
| `1y` | 365 | 日线 | 46 KB | |

返回：`{"candles":[{at,open,high,low,close,volume}], "currency", "updated_at"}`，`at` 为 UTC ISO8601。
**没有逐笔成交、没有盘口深度**（crypto 的 bid/ask 只在 `state.json` 的 `quote` 里）。

> **归档（archived）**：一部分标的返回 `"archived": true` + 几个月前的旧 K 线（2026-10-23 实测：CN/HK/JP/SG/EU 里约一半，如 `TSE:7203` 只到 2026-07-17、`INDEX:DAX` 只到 2026-07-14），而同一标的在 `state.json` 里的 `quote.received_at`/sparkline 仍在更新。归档会自愈（`AMEX:PPLT` 30 分钟后恢复成当天的 5m 线），所以客户端应降频复查而不是放弃。判断"活"的口径：`archived is not True` 且最后一根 K 线距今 < 72h。

### 3.3 下单（核心）

```
POST /rsc/orders.json
     instrument_id=11
     side=long|short|close
     quantity=0.00001
     leverage=1
     request_id=<uuid4>            # 幂等键，必填
     take_profit=...  stop_loss=... # 可选
     high_risk=true|false           # 可选：杠杆>10 的专项额度
   -> {"order_id":5583,"status":"pending","replayed":false}
```

实测（2026-10-23 12:48 UTC）：

```json
// 请求 BTC 0.00001 多单 1x
POST /rsc/orders.json -> {"order_id":5583,"status":"pending","replayed":false}

// 成交后 state.orders 中
{"id":5583,"symbol":"CRYPTO:BTC/USD","side":"long","status":"filled",
 "quantity":"0.00001","leverage":1,"created_at":"2026-10-23T12:48:38.608Z",
 "execute_at":"2026-10-23T12:48:38.607Z","expires_at":"2026-10-23T13:03:38.607Z",
 "can_cancel":false,"error_message":false,
 "details":{"price":"81662.92","fee":"0.0008166292","margin":"0.8166292",
            "gross":"0.8166292","reference_price":"81665.81"}}
```

要点：
- `status` 先返回 `pending`，**数秒后**才在 state 中变 `filled`（crypto 需下一 tick 报价确认）。
- 成交价 = `reference_price` ± 极小滑点（81662.92 vs 81665.81，约 0.36 bp）。
- `fee = gross × fee_bps/10000`（本例 0.8166292 × 10bp = 0.0008166292，分毫不差）。
- `replayed` 字段用于幂等重放识别 → 重试安全。实测：同一 `request_id` 再发一次返回 `{"order_id":5586,"status":"pending","replayed":true}`，**不产生第二笔**。
- 内联 `take_profit`/`stop_loss` 实测生效：下单后持仓直接带上 `tp=85698.19, sl=79168.8`。

### 3.4 平仓

复用下单接口，`side=close`，`quantity` 为持仓数量，`leverage` 必须与原仓一致：

```json
POST side=close quantity=0.00001 leverage=1
-> {"order_id":5584,"status":"pending"}
// 成交后 details：
{"price":"81652.14","fee":"0.0008165214","pnl":"-0.0001078",
 "payout":"0.8157048786","margin":"0.8166292","gross":"0.8165214",
 "reason":"stock_closed","reference_price":"81658.36"}
```

→ 往返成本 ≈ 2 × fee_bps，**这是所有策略的最低门槛**。

### 3.5 撤单

```
POST /rsc/orders/{id}/cancel.json
     request_id=<uuid4>          # 必填！
```

| 场景 | 返回 |
|---|---|
| 缺 `request_id` | `400 {"errors":["param is missing ... request_id"]}` |
| 已成交/已撤 | `409 {"errors":["Order not pending."],"error_code":"order_not_pending"}` |
| **成功** | `200 {"order_id":5589,"status":"canceled","replayed":false}` ← 注意是 **canceled**，单 l |

### 3.6 止盈止损

```
POST /rsc/positions/{id}/protection.json
     take_profit=<price>&stop_loss=<price>&request_id=<uuid4>   # request_id 必填！
```

| 场景 | 实测返回 |
|---|---|
| 成功 | `200 {"position_id":525,"replayed":false}` |
| 清空 | `take_profit=""&stop_loss=""` → 200，持仓字段变回 `null` |
| 止盈低于盈亏平衡 | `422 {"errors":["Invalid protection."],"error_code":"invalid_protection"}` |
| 止损越过强平价 | `422 {"errors":["Stop-loss must trigger before the liquidation price."],"error_code":"stop_loss_beyond_liquidation"}` |
| 行情陈旧 | `409 {"errors":["Quote stale."],"error_code":"quote_stale"}` |
| 缺 request_id | `400 {"errors":["param is missing ... request_id"]}` |

**关键**：下单时可以直接内联 `take_profit`/`stop_loss`（实测生效，持仓创建后即带上），不必"先开后设"，可少一次请求、少一个竞态窗口。

### 3.7 其他

| 接口 | 用途 | 备注 |
|---|---|---|
| `POST /rsc/instruments/{id}/refresh.json` | 强制刷新单标的行情 | 有外部源调用成本，慎用 |
| `POST /rsc/search.json {q}` | 搜索标的 | 冷门标的发现用 |
| `GET /rsc/ranking.json?sort=pnl\|equity` | 排行榜 | 返回 `rows[{return_pct,equity,realized_pnl,total_pnl,trade_count,...}]`，**用作验收基准** |
| `GET /rsc/traders/{id}.json` | 他人持仓/绩效 | 信息面研究 |
| `GET /rsc/history.json?per_page=20` | 钱包流水 | `operation: daily_reward/stock_fill/stock_reserve/...` |
| `GET /rsc/forecast/state.json` | 事件预测（Polymarket 类） | 非本期范围 |
| `POST /rsc/predictions.json` | 赛事预测 | 非本期范围 |

### 3.8 错误码字典（2026-10-23 全量实测）

`error_code` 是**机器可读**的，`errors[]` 是给人看的（中文，编码可能乱码）。风控/执行器只依赖 `error_code`。

| HTTP | error_code | 触发条件（实测） | 执行器应做什么 |
|---|---|---|---|
| 409 | `market_closed` | 闭市时下单（NASDAQ:SNDK） | 丢弃信号，标的加入冷却 |
| 409 | `position_conflict` | ①反向开仓 ②已有持仓时换杠杆加仓 ③同品种已有同类持仓 | 先平后开，或改参数 |
| 409 | `quote_stale` | 报价过期（延迟品种/刚开盘/休市前） | 刷新/等待，**不可重试** |
| 409 | `order_not_pending` | 撤单时单已成交或已撤 | 视为成功，重新对账 |
| 422 | `invalid_order` | 杠杆 > max_leverage（101x） | 参数错误，不重试 |
| 422 | `invalid_quantity` | 数量低于 minimum（0.0000005 < 0.000001） | 参数错误，不重试 |
| 422 | `invalid_protection` | 止盈低于盈亏平衡价 | 重算后重试一次 |
| 422 | `stop_loss_beyond_liquidation` | 止损越过强平价 | 重算后重试一次 |
| 400 | —（无 error_code） | 缺 `request_id`（下单/撤单/保护单都会） | 代码 bug，告警 |
| 200 | `replayed: true` | 同 request_id 重复提交 | 视为成功，用返回的 order_id |

**重要**：`quote_stale` 会在**开市前、休市后、延迟品种**上大面积出现（实测 13:00 UTC 时美股全部因此被拒，而非 `market_closed`）。执行器必须把"行情新鲜度"作为下单前置条件，而不是等错误码回来再处理。

---

## 4. 交易规则（硬约束，风控引擎必须实现）

来源：topic 28918《RSC 交易规则限制公示 v0.1》+ 管理员回复 + 实测。

### 4.1 杠杆与方向

| 规则 | 值 |
|---|---|
| 普通品种杠杆 | 1–10x |
| 虚拟币 / 外汇 | 最高 100x |
| 高杠杆专项额度 | 杠杆 > 10x 的持仓与待成交开仓**共用**账户权益 25% 的初始保证金（`high_risk.budget.maximum_margin`） |
| 双向持仓 | **禁止**同标的同时多空 |
| 加仓 | 必须同方向、同杠杆 |
| 换向/换杠杆 | 必须先平仓 |

实测 `high_risk`：`{"equity":"608.13","maximum_margin":"152.03","used_margin":"0","available_margin":"152.03","margin_percent":25}`。

### 4.2 数量与流动性

| 规则 | 值 |
|---|---|
| 最小数量 | crypto `0.000001`；其他 `0.01`；须为 `step` 整数倍 |
| 股票最小名义 | ≥ 1 RSC |
| 大额限制 | 超过流动性上限会被拒或截断 |

### 4.3 交易时段

未在时段内不得开/平仓（`market_closed: true` 时）。当前 1638 个标的中 **1277 个处于闭市**。

| 交易所 | UTC 时段 | 北京时间 |
|---|---|---|
| NYSE/NASDAQ/AMEX | 13:30–20:00 | 21:30–04:00 |
| XETRA/LSE/EPA/AMS/SIX/EURONEXT 等欧股 | 07:00–15:30 | 15:00–23:30 |
| TSE | 00:00–06:30 | 08:00–14:30 |
| HKEX | 01:30–08:10（含午休） | 09:30–16:10 |
| SSE/SZSE | 01:30–07:00 | 09:30–15:00 |
| SGX | 01:00–09:00 | 09:00–17:00 |
| FX | 22:00–22:59（近 24h） | — |
| CRYPTO | 24×7 | — |

> 注意夏令时：时段随 DST 平移，必须以 `quote.session_start/end` 为准，**不要硬编码**。

### 4.4 费用（每边，按名义金额）

| 类别 | fee_bps | 标的数 |
|---|---|---|
| 外汇 forex | **1** | 12 |
| 美股 us | 5 | 518 |
| 指数 indices | 5 | 16 |
| 贵金属 metals | 5 | 4 |
| 港股 hk | 10 | 181 |
| 日股 jp | 10 | 225 |
| 欧股 eu | 10 | 318 |
| 新加坡 sg | 10 | 30 |
| A股 cn | 10（7 只 13） | 307 |
| 加密 crypto | 10（2 只 20） | 27 |

→ **往返成本 = 2 × fee_bps**：外匯 2bp、美股 10bp、港股/日股/欧股 20bp、加密 20bp。
→ 20bp 往返意味着**日内策略必须日均波动 > 0.4% 才有正期望**，这条直接决定策略池设计（见 [05](05-strategy-and-backtest.md)）。

### 4.5 强平与保护

实测公式（8 个持仓全部吻合）：

```
maintenance = margin × 0.25
多单 liquidation = average × (1 − 0.75 / leverage)
空单 liquidation = average × (1 + 0.75 / leverage)
```

验证：NVDA `avg 199, lev 1 → liq 49.75`；QQQ `710.62 → 177.655`；FX:JPY `0.0063826, lev 20 → 0.0061433`。

规则：仓位权益 ≤ 保证金 25% 触发强平；止损必须早于强平价；止盈必须覆盖盈亏平衡价（`break_even`）。

### 4.6 成交机制（最容易踩坑）

1. 下单**不保证立即成交**。
2. 虚拟币需等待提交后的**新报价**（我们实测约 1–3 秒）；延迟行情品种需**再等一个更新**（15 分钟级）。
3. 报价失效或价格变动 **> 5%** → 不成交。
4. 待成交开仓额外预留约 5% 资金（`reserved`）。
5. **撤单窗口**：虚拟币成交前可撤；其他品种通常仅提交后 **10 秒**内可撤（`cancel_until`）。
6. 超时释放：crypto 15 分钟、其他 30 分钟（`expires_at`）。

### 4.7 限频（服务端硬限制，必须做客户端令牌桶预限）

| 动作 | 限制 |
|---|---|
| 行情/下单请求 | **30 次/分钟** |
| 事件预测询价 | 12 次/分钟 |
| 事件预测确认 | 10 次/分钟 |
| 转账/打赏/红包合计 | 20 次/天、300 RSC/天 |

→ 30 次/分钟 = **每 2 秒 1 次**。这个额度是系统设计的核心稀缺资源：一次 tick 想同时看 10 个标的，额度立刻见底。**架构上必须"少请求、多复用"**（见 [02](02-architecture.md) §4）。

---

## 5. 数据模型

### 5.1 instrument（1638 个）

```
id, symbol, display_symbol, exchange, currency, name, category,
quote{}, market_closed, minimum, step, minimum_notional(all null),
execution_prices{long,short}, popularity, last_order_at, catalog_rank,
catalog_id, fee_bps, fx_activity, margin_budget_exempt, max_leverage,
currency_name, close_only, execution_mode, history[16]
```

分布（实测）：

| 维度 | 分布 |
|---|---|
| category | us 518 / eu 318 / cn 307 / jp 225 / hk 181 / sg 30 / crypto 27 / indices 16 / forex 12 / metals 4 |
| exchange | NYSE 355 / TSE 225 / SSE 193 / HKEX 181 / NASDAQ 158 / SZSE 114 / LSE 68 / XETRA 43 / EPA 39 / SIX 32 / SGX 30 / CRYPTO 27 / … |
| execution_mode | immediate 1560 / delayed_confirmation 39 / crypto_confirmation 27 / forex_confirmation 12 |
| max_leverage | crypto·forex 100，其余 10 |
| close_only | 6 个（DRM3/DRMS/MVLL/MUU/HYNX/TSMU，均为杠杆/反向基金） |

### 5.2 quote（价格来源是理解一切的关键）

```
price, bid, ask(crypto), low, high, open, previous_close,
local_price, local_currency, fx_rate, inverse_rate(FX), base_currency,
source(yahoo 1610 / coinbase_ws 20 / coinbase 3 / okx 2 / kraken 2),
received_at, source_time, session_start, session_end,
delay_seconds, delay_inferred, delay_reported, delay_observations,
change_basis(previous_close|24h), pricing_method(fx_reciprocal_v1)
```

**定价恒等式**：

```
price(RSC) = local_price × fx_rate        # RSC 锚定 USD
0700.HK : 387.20 HKD × 0.127541 = 49.38
600519  : 1328.31 CNY × 0.149889 = 199.10
8306    : 3282 JPY  × 0.006371 = 20.91
HSBA    : 1347.4 GBp × 0.013376 = 18.02
```

这条恒等式产生两个**可研究 alpha 假设**：
1. `fx_rate` 是服务端固定换算率（非实时 FX）→ 当真实汇率大幅波动时，本地价格与 RSC 价格出现背离 → **跨市场配对交易机会**（用 `FX:*` 对冲）。
2. 同资产多挂牌（`EURONEXT:ASML` vs `AMS:ASML`）→ 同一 underlying 两个报价，可做**价差回归**。

### 5.3 延迟分布（决定策略可用市场）

| delay_seconds | 数量 | 交易所 |
|---|---|---|
| 0 | 1598 | 美股、指数、crypto、forex、部分欧股 |
| 606–610 | 2 | SGX |
| 903–930 | 37 | SSE 10、SZSE 8、TSE 8、HKEX 5、XETRA 2、LSE 2、EURONEXT 1、EPA 1 |
| 缺失 | 1 | `delay_seconds` 为 null，按延迟品种处理 |

合计 1598 + 2 + 37 + 1 = 1638。延迟品种（>0 或缺失）共 40 个，其中 15 分钟级 37 个。

`delay_inferred: true` 表示服务端从 `received_at − source_time` 推断，说明上游是 Yahoo 延迟源。

### 5.4 position / order / wallet

```
position: id, instrument_id, symbol, side, leverage, quantity, average, margin,
          pnl, equity, maintenance, fee_bps, break_even, risk, valuation_basis,
          valuation_at, take_profit, stop_loss, hold_until, liquidation, pending_open
order:    id, instrument_id, symbol, side, status(pending|filled|cancelled|rejected),
          quantity, leverage, reserved, created_at, execute_at, expires_at,
          cancel_at, cancel_until, can_cancel, error_message,
          details{price,fee,margin,gross,pnl,payout,reference_price,reason}
wallet:   balance, status(active|frozen), status_reason, reserved
portfolio:notional, reserved, margin, pnl, equity
```

当前账户基线（2026-10-23 12:50 UTC）：余额 **608.07 RSC**，9 个持仓（NVDA/AMD/QQQ/GLD/AAPL/MU/TSLA/FX:JPY/BTC 测试仓已平），每日活跃奖励 +10 RSC（`daily_reward`，UTC 18:00 入账）。

---

## 6. 坑位清单（写代码前必读）

1. **必须邮箱登录**，中文用户名会 400。
2. 登录失败也返回 200，必须检查 body。
3. `request_id` 在**下单和撤单**都是必填，缺失 400。
4. 下单返回 `pending` 不代表成交，必须轮询/跟踪状态机。
5. `side=close` 时 `leverage` 必须与原仓一致，否则拒单。
6. 撤单窗口只有 10 秒（非 crypto），超时只能等成交或超时释放。
7. 全量 `state.json` 3.4 MB / 4.4 s —— 用它做 1 分钟轮询会直接打满限频且拖垮服务端。
8. `section=market` 只给 27 个热门标的，**不是全部**；自选池要用 `history.json`。
9. 行情时间戳是 UTC，交易所时段随 DST 变化，**禁止硬编码时区偏移**。
10. 延迟品种（40 个）的价格是 15 分钟前的，**任何 < 15 分钟的动量信号都是噪声**。
11. `market_closed` 每日变化（实测某一刻 1277/1638 闭市），调度器必须动态读。
12. 排行榜 `sort` 只接受 `pnl` / `equity`，其他 422。
13. `1m`/`3m` 的 K 线接口 422，历史粒度只有 5m/1h/1d。
14. 社区站可能随时修改规则；**所有规则必须配置化 + 契约测试守护**。
