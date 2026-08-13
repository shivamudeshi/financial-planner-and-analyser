"""Return metrics.

XIRR is the headline number here. With a SIP running, the fund's advertised
"3-year return" is a point-to-point figure on a lump sum you never invested;
your actual money-weighted return is XIRR over your real cashflows, and it is
often materially different.
"""

from __future__ import annotations

import sqlite3
from datetime import date as Date

from ..money import pct

DAYS_PER_YEAR = 365.0


def xirr(cashflows: list[tuple[str, float]], *, guess: float = 0.1) -> float | None:
    """Money-weighted annualised return for dated cashflows.

    Convention: investments negative, redemptions and the closing value positive.
    Newton's method with a bisection fallback, because Newton diverges on the
    sign patterns a real SIP ledger produces.
    """
    if len(cashflows) < 2:
        return None
    flows = sorted(cashflows, key=lambda c: c[0])
    if not (any(a < 0 for _, a in flows) and any(a > 0 for _, a in flows)):
        return None  # no sign change: no root to find

    t0 = Date.fromisoformat(flows[0][0])
    years = [(Date.fromisoformat(d) - t0).days / DAYS_PER_YEAR for d, _ in flows]
    amounts = [a for _, a in flows]

    def npv(rate: float) -> float:
        if rate <= -1:
            return float("inf")
        return sum(a / (1 + rate) ** t for a, t in zip(amounts, years))

    rate = guess
    for _ in range(100):
        f = npv(rate)
        if abs(f) < 1e-6:
            return rate
        step = 1e-6
        derivative = (npv(rate + step) - f) / step
        if derivative == 0:
            break
        new_rate = rate - f / derivative
        if new_rate <= -0.9999:
            break
        if abs(new_rate - rate) < 1e-9:
            return new_rate
        rate = new_rate

    lo, hi = -0.9999, 10.0
    if npv(lo) * npv(hi) > 0:
        return None
    for _ in range(300):
        mid = (lo + hi) / 2
        if npv(lo) * npv(mid) <= 0:
            hi = mid
        else:
            lo = mid
    return (lo + hi) / 2


def portfolio_xirr(
    conn: sqlite3.Connection,
    instrument_id: int | None = None,
    as_of: str | None = None,
) -> float | None:
    """XIRR across real transactions, closing with today's market value."""
    as_of = as_of or Date.today().isoformat()
    sql = (
        "SELECT date, kind, amount, charges FROM transactions WHERE date <= ?"
    )
    args: list = [as_of]
    if instrument_id is not None:
        sql += " AND instrument_id = ?"
        args.append(instrument_id)

    flows: list[tuple[str, float]] = []
    for r in conn.execute(sql + " ORDER BY date", args):
        amt = abs(r["amount"])
        if r["kind"] in ("BUY", "SIP", "SWITCH_IN"):
            flows.append((r["date"], -(amt + r["charges"])))
        elif r["kind"] in ("SELL", "SWITCH_OUT"):
            flows.append((r["date"], amt - r["charges"]))
        elif r["kind"] == "DIVIDEND":
            flows.append((r["date"], amt))

    from ..portfolio import positions

    value = sum(
        p.market_value
        for p in positions(conn, as_of)
        if instrument_id is None or p.instrument_id == instrument_id
    )
    if value:
        flows.append((as_of, float(value)))
    return xirr(flows)


def absolute_return(cost: int, value: int) -> float:
    return pct(value - cost, cost)


def cagr(cost: int, value: int, days: int) -> float | None:
    if cost <= 0 or days <= 0:
        return None
    return ((value / cost) ** (DAYS_PER_YEAR / days) - 1) * 100


def drawdown_series(closes: list[int]) -> list[float]:
    """Percentage below the running peak, for a drawdown chart."""
    out, peak = [], closes[0] if closes else 0
    for c in closes:
        peak = max(peak, c)
        out.append(pct(c - peak, peak))
    return out


def max_drawdown(closes: list[int]) -> float:
    return min(drawdown_series(closes), default=0.0)
