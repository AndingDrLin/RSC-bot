"""M3 撮合与组合：论文账户的成交、保证金、强平，规则与交易所对齐。

对齐依据（2026-10-23 用真实账户实测，见 docs/01-exchange-contract.md）：
  费用    fee = 名义价值 × fee_bps / 10000，开平各收一次（BTC 单验证到 1e-12）
  保证金  margin = 名义价值 / 杠杆
  强平    equity - margin <= -0.75 × margin 时爆仓
          即 long 强平价 = entry × (1 - 0.75/L)，short = entry × (1 + 0.75/L)
          （FX:JPY 20x：0.0063826 × (1 - 0.0375) = 0.00614325，与交易所 0.006143 一致）
  滑点    |price / reference - 1| > 5% 拒单（交易所的 5% 漂移保护）
  账务    balance 是"含已占用保证金"的余额，equity = balance + 未实现盈亏，
          可用 = balance - Σ margin（与 state.wallet/portfolio 的口径一致）

这个模块只做"钱"，不做策略、不做风控规则（那是 M4）。所有函数无随机性，
同样的输入永远得到同样的输出，方便回测复现。
"""

from __future__ import annotations

from dataclasses import dataclass, field

MAINTENANCE_RATIO = 0.25      # 维持保证金 = 0.25 × 初始保证金
MAX_PRICE_DRIFT = 0.05        # 成交价相对参考价漂移上限


class Reject(Exception):
    """下单被拒。code 用于对账/日志，reason 给人看。"""

    def __init__(self, code: str, reason: str):
        super().__init__(f"{code}: {reason}")
        self.code = code
        self.reason = reason


@dataclass
class Fill:
    ts: int
    symbol: str
    side: str            # open 的 side: long/short；close 时是平仓方向
    action: str          # open / close / liquidate
    quantity: float
    price: float
    fee: float
    leverage: float
    margin: float
    pnl: float | None = None
    reason: str = ""

    @property
    def notional(self) -> float:
        return abs(self.quantity) * self.price


@dataclass
class Position:
    symbol: str
    side: str            # long / short
    quantity: float
    entry: float
    leverage: float
    fee_bps: float
    opened_ts: int
    stop_loss: float | None = None
    take_profit: float | None = None

    @property
    def sign(self) -> int:
        return 1 if self.side == "long" else -1

    @property
    def margin(self) -> float:
        return self.quantity * self.entry / self.leverage

    def pnl(self, price: float) -> float:
        return (price - self.entry) * self.quantity * self.sign

    def liquidation_price(self) -> float:
        """equity - margin <= -0.75 margin 的临界价。"""
        move = (1.0 - MAINTENANCE_RATIO) / self.leverage
        return self.entry * (1 - move) if self.side == "long" else self.entry * (1 + move)

    def hit(self, price: float) -> str | None:
        """返回触发原因：liquidation / stop_loss / take_profit。"""
        liq = self.liquidation_price()
        if self.side == "long":
            if price <= liq:
                return "liquidation"
            if self.stop_loss is not None and price <= self.stop_loss:
                return "stop_loss"
            if self.take_profit is not None and price >= self.take_profit:
                return "take_profit"
        else:
            if price >= liq:
                return "liquidation"
            if self.stop_loss is not None and price >= self.stop_loss:
                return "stop_loss"
            if self.take_profit is not None and price <= self.take_profit:
                return "take_profit"
        return None


class PaperBroker:
    """单账户。不处理跨币种换算：价格与费用统一按标的计价货币记账。"""

    def __init__(self, balance: float = 1000.0, max_drift: float = MAX_PRICE_DRIFT):
        self.balance = float(balance)
        self.max_drift = max_drift
        self.positions: dict[str, Position] = {}
        self.fills: list[Fill] = []
        self.realized_pnl = 0.0
        self.fees_paid = 0.0

    # ── 账户视图 ────────────────────────────────────────
    @property
    def margin_used(self) -> float:
        return sum(p.margin for p in self.positions.values())

    @property
    def available(self) -> float:
        return self.balance - self.margin_used

    def unrealized(self, prices: dict[str, float]) -> float:
        total = 0.0
        for sym, p in self.positions.items():
            px = prices.get(sym)
            if px is not None:
                total += p.pnl(px)
        return total

    def equity(self, prices: dict[str, float]) -> float:
        return self.balance + self.unrealized(prices)

    # ── 成交 ────────────────────────────────────────────
    def open(self, ts: int, symbol: str, side: str, quantity: float, price: float,
             leverage: float = 1.0, fee_bps: float = 0.0,
             reference: float | None = None, stop_loss: float | None = None,
             take_profit: float | None = None) -> Fill:
        if side not in ("long", "short"):
            raise Reject("bad_side", f"side={side}")
        if quantity <= 0:
            raise Reject("bad_quantity", f"quantity={quantity}")
        if leverage < 1:
            raise Reject("bad_leverage", f"leverage={leverage}")
        if price <= 0:
            raise Reject("bad_price", f"price={price}")
        if reference is not None and abs(price / reference - 1) > self.max_drift:
            raise Reject("price_drift", f"{price} vs ref {reference}")
        if symbol in self.positions:
            raise Reject("symbol_busy", f"{symbol} already open (v1 不做加仓)")

        notional = quantity * price
        margin = notional / leverage
        fee = notional * fee_bps / 10000.0
        if margin + fee > self.available + 1e-12:
            raise Reject("insufficient_margin",
                         f"need {margin + fee:.6f}, have {self.available:.6f}")

        self.balance -= fee
        self.fees_paid += fee
        pos = Position(symbol, side, quantity, price, leverage, fee_bps, ts,
                       stop_loss=stop_loss, take_profit=take_profit)
        self.positions[symbol] = pos
        fill = Fill(ts, symbol, side, "open", quantity, price, fee, leverage, margin)
        self.fills.append(fill)
        return fill

    def close(self, ts: int, symbol: str, price: float, quantity: float | None = None,
              reason: str = "manual") -> Fill:
        pos = self.positions.get(symbol)
        if pos is None:
            raise Reject("no_position", f"{symbol} not open")
        qty = pos.quantity if quantity is None else min(quantity, pos.quantity)
        if qty <= 0:
            raise Reject("bad_quantity", f"quantity={quantity}")

        notional = qty * price
        fee = notional * pos.fee_bps / 10000.0
        pnl = (price - pos.entry) * qty * pos.sign
        margin_release = pos.margin * (qty / pos.quantity)

        self.balance += pnl - fee
        self.realized_pnl += pnl
        self.fees_paid += fee
        if qty >= pos.quantity - 1e-15:
            self.positions.pop(symbol, None)
            closed = pos.quantity
        else:
            pos.quantity -= qty
            closed = qty
        fill = Fill(ts, symbol, pos.side, "close", closed, price, fee, pos.leverage,
                    margin_release, pnl=pnl, reason=reason)
        self.fills.append(fill)
        return fill

    def check_exits(self, ts: int, prices: dict[str, float]) -> list[Fill]:
        """逐标的检查强平/止盈止损，触发即按该价格平仓。"""
        out = []
        for sym in list(self.positions):
            px = prices.get(sym)
            if px is None:
                continue
            why = self.positions[sym].hit(px)
            if why:
                out.append(self.close(ts, sym, px, reason=why))
        return out

    # ── 组合指标 ────────────────────────────────────────
    def snapshot(self, ts: int, prices: dict[str, float]) -> dict:
        return {
            "ts": ts,
            "balance": self.balance,
            "margin": self.margin_used,
            "unrealized": self.unrealized(prices),
            "equity": self.equity(prices),
            "positions": len(self.positions),
            "realized_pnl": self.realized_pnl,
            "fees_paid": self.fees_paid,
        }

    def trade_log(self) -> list[dict]:
        """成对成交（open/close）整理成一次往返，供指标使用。"""
        trades: list[dict] = []
        openers: dict[str, Fill] = {}
        for f in self.fills:
            if f.action == "open":
                openers[f.symbol] = f
            elif f.action in ("close", "liquidate") and f.symbol in openers:
                o = openers.pop(f.symbol)
                notional_in = o.notional
                trades.append({
                    "symbol": f.symbol,
                    "side": o.side,
                    "quantity": f.quantity,
                    "entry": o.price,
                    "exit": f.price,
                    "pnl": f.pnl,
                    "fees": o.fee + f.fee,
                    "ret": (f.pnl / notional_in) if notional_in else 0.0,
                    "ts_open": o.ts,
                    "ts_close": f.ts,
                    "reason": f.reason,
                })
        return trades
