"""M3-T4 回测指标。纯函数、无可选依赖，输入是等间隔的权益序列。

年化口径：periods_per_year 由调用方给（5m bar × 24h × 365 天 = 105120；日线 252）。
夏普用无风险利率 0 的简化定义：mean(r) / std(r) × sqrt(periods_per_year)。
回撤看的是权益峰值到谷底，不做时间加权。
"""

from __future__ import annotations

import math


def returns(equity: list[float]) -> list[float]:
    out = []
    for prev, cur in zip(equity, equity[1:]):
        if prev:
            out.append(cur / prev - 1.0)
    return out


def total_return(equity: list[float]) -> float:
    if not equity or not equity[0]:
        return 0.0
    return equity[-1] / equity[0] - 1.0


def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def _std(xs: list[float]) -> float:
    if len(xs) < 2:
        return 0.0
    m = _mean(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def sharpe(equity: list[float], periods_per_year: float = 252.0) -> float:
    r = returns(equity)
    sd = _std(r)
    if sd == 0:
        return 0.0
    return _mean(r) / sd * math.sqrt(periods_per_year)


def sortino(equity: list[float], periods_per_year: float = 252.0) -> float:
    r = returns(equity)
    downside = [x for x in r if x < 0]
    if not downside:
        return 0.0
    dd = math.sqrt(sum(x * x for x in downside) / len(downside))
    if dd == 0:
        return 0.0
    return _mean(r) / dd * math.sqrt(periods_per_year)


def max_drawdown(equity: list[float]) -> tuple[float, int, int]:
    """返回 (最大回撤, 峰值下标, 谷底下标)，回撤为正数。"""
    peak = -math.inf
    peak_i = 0
    worst = 0.0
    worst_peak = worst_trough = 0
    for i, v in enumerate(equity):
        if v > peak:
            peak, peak_i = v, i
        if peak > 0:
            dd = 1.0 - v / peak
            if dd > worst:
                worst, worst_peak, worst_trough = dd, peak_i, i
    return worst, worst_peak, worst_trough


def calmar(equity: list[float], periods_per_year: float = 252.0) -> float:
    dd, _, _ = max_drawdown(equity)
    if dd == 0 or len(equity) < 2:
        return 0.0
    years = (len(equity) - 1) / periods_per_year
    if years <= 0:
        return 0.0
    annual = (1.0 + total_return(equity)) ** (1.0 / years) - 1.0
    return annual / dd


def cagr(equity: list[float], periods_per_year: float = 252.0) -> float:
    if len(equity) < 2 or equity[0] <= 0:
        return 0.0
    years = (len(equity) - 1) / periods_per_year
    return (equity[-1] / equity[0]) ** (1.0 / years) - 1.0 if years > 0 else 0.0


def trade_stats(trades: list[dict]) -> dict:
    """trades: [{'pnl', 'fees', 'ret', 'notional'?}]，一次开平算一笔。"""
    if not trades:
        return {"trades": 0, "win_rate": 0.0, "profit_factor": 0.0,
                "avg_win": 0.0, "avg_loss": 0.0, "fees": 0.0, "turnover": 0.0}
    pnls = [t.get("pnl") or 0.0 for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    gross_win = sum(wins)
    gross_loss = -sum(losses)
    fees = sum(t.get("fees") or 0.0 for t in trades)
    notional = sum(t.get("notional") or 0.0 for t in trades)
    return {
        "trades": len(trades),
        "win_rate": len(wins) / len(trades),
        # 没有亏损时 profit_factor 无意义，用 inf 会污染 JSON，用 0 表示"无亏损样本"
        "profit_factor": (gross_win / gross_loss) if gross_loss else 0.0,
        "avg_win": _mean(wins),
        "avg_loss": _mean(losses),
        "fees": fees,
        "turnover": notional,
    }


def summary(equity: list[float], trades: list[dict] | None = None,
            periods_per_year: float = 252.0) -> dict:
    dd, peak_i, trough_i = max_drawdown(equity)
    out = {
        "bars": len(equity),
        "final_equity": equity[-1] if equity else 0.0,
        "total_return": total_return(equity),
        "cagr": cagr(equity, periods_per_year),
        "sharpe": sharpe(equity, periods_per_year),
        "sortino": sortino(equity, periods_per_year),
        "calmar": calmar(equity, periods_per_year),
        "max_drawdown": dd,
        "max_drawdown_span": [peak_i, trough_i],
        "volatility": _std(returns(equity)) * math.sqrt(periods_per_year),
    }
    if trades is not None:
        out["trade"] = trade_stats(trades)
    return out
