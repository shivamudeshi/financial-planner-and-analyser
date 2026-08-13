"""Builds the fact dictionary a rule condition is evaluated against.

Every field a rule can name is assembled here, which makes this module the
answer to "what can I write a rule about?" — `fpa rules fields` prints
:data:`FIELDS` straight from it, so the documentation cannot drift from the
implementation.

Readings that are genuinely unavailable are ``None`` rather than zero. A fund
with three weeks of NAV history has no 200-day average, and ``sma200 = 0`` would
make every "price above its 200 DMA" rule fire. ``None`` makes the rule not fire,
which is the honest answer.
"""

from __future__ import annotations

import sqlite3
from datetime import date as Date

from ..analysis import technicals
from ..analysis.returns import portfolio_xirr
from ..money import pct
from ..portfolio import Position, positions
from ..tax.engine import TaxEngine, Term

# name -> what it means. Printed by `fpa rules fields`.
FIELDS: dict[str, str] = {
    # identity
    "name": "Instrument name",
    "symbol": "Ticker, or '' for funds",
    "kind": "'EQUITY' or 'MF'",
    "category": "Sector for equities, scheme category for funds",
    "asset_class": "'EQUITY', 'DEBT', 'HYBRID', 'GOLD'",
    "exit_priority": "0-100 conviction to exit, set on the Holdings page",
    # position
    "quantity": "Units or shares held",
    "price": "Latest close or NAV, in rupees",
    "market_value": "Position value in rupees",
    "cost": "Cost basis in rupees",
    "unrealised": "Unrealised gain in rupees",
    "unrealised_pct": "Unrealised gain as a percent of cost",
    "weight_pct": "Position as a percent of total portfolio value",
    "days_held": "Age of the oldest open lot, in days",
    "newest_days_held": "Age of the newest open lot, in days",
    # tax
    "short_term_gain": "Unrealised gain sitting in lots held 12 months or less",
    "long_term_gain": "Unrealised gain in lots held over 12 months",
    "days_to_long_term": "Days until the newest lot turns long-term (0 if all are)",
    "all_long_term": "True when every open lot is over 12 months old",
    # price behaviour (equities; None for funds)
    "close": "Latest close in rupees",
    "sma20": "20-day simple moving average",
    "sma50": "50-day simple moving average",
    "sma200": "200-day simple moving average",
    "rsi14": "14-day Wilder RSI",
    "macd_hist": "MACD histogram (12, 26, 9)",
    "atr14": "14-day average true range",
    "high_52w": "52-week high",
    "low_52w": "52-week low",
    "from_52w_high": "Percent below the 52-week high (negative)",
    "above_200dma": "True when the close is above the 200-day average",
    "trend": "'Uptrend', 'Downtrend' or 'Insufficient history'",
    "volatility_pct": "Annualised volatility of daily returns, percent",
    "peak_since_buy": "Highest close since the oldest lot was bought",
    "drawdown_from_peak": "Percent below that peak (negative)",
    # funds
    "xirr_pct": "Money-weighted return on this holding's actual cashflows",
    # guards
    "has_price_history": "True when there is enough history for indicators",
    # portfolio scope
    "total_value": "Whole-portfolio value in rupees",
    "actual_pct": "Actual share of portfolio for this asset class",
    "target_pct": "Target share from plan_targets",
    "drift_pct": "actual_pct minus target_pct",
}


def _rupees(paise: int) -> float:
    return paise / 100


def instrument_context(
    conn: sqlite3.Connection,
    position: Position,
    *,
    engine: TaxEngine,
    total_value: int,
    as_of: str,
    with_technicals: bool = True,
) -> dict:
    """Facts about one holding."""
    by_term = position.unrealised_by_term(engine)
    lots = position.lots
    days = [l.days_held for l in lots]
    to_lt = [l.days_to_long_term(engine) for l in lots]

    ctx: dict = {
        "name": position.name,
        "symbol": position.symbol,
        "kind": position.kind,
        "category": position.category,
        "asset_class": position.asset_class,
        "exit_priority": position.exit_priority,
        "quantity": position.quantity,
        "price": _rupees(position.price),
        "market_value": _rupees(position.market_value),
        "cost": _rupees(position.cost),
        "unrealised": _rupees(position.gain),
        "unrealised_pct": position.gain_pct,
        "weight_pct": pct(position.market_value, total_value),
        "days_held": max(days) if days else 0,
        "newest_days_held": min(days) if days else 0,
        "short_term_gain": _rupees(by_term[Term.SHORT]),
        "long_term_gain": _rupees(by_term[Term.LONG]),
        "days_to_long_term": max(to_lt) if to_lt else 0,
        "all_long_term": all(t == 0 for t in to_lt) if to_lt else False,
        "xirr_pct": None,
        "has_price_history": False,
        # Technical readings default to None so a rule naming them simply does
        # not fire when the data is not there.
        **{k: None for k in (
            "close", "sma20", "sma50", "sma200", "rsi14", "macd_hist", "atr14",
            "high_52w", "low_52w", "from_52w_high", "above_200dma", "volatility_pct",
            "peak_since_buy", "drawdown_from_peak",
        )},
        "trend": "Insufficient history",
    }

    if not with_technicals:
        return ctx

    if position.kind == "EQUITY":
        # Deliberately equity-only: a NAV series has no volume or order flow, so
        # momentum indicators on it describe arithmetic, not market behaviour.
        snap = technicals.snapshot(conn, position.instrument_id, as_of)
        if snap:
            ctx.update(
                close=_rupees(snap.close),
                sma20=_rupees(snap.sma20) if snap.sma20 else None,
                sma50=_rupees(snap.sma50) if snap.sma50 else None,
                sma200=_rupees(snap.sma200) if snap.sma200 else None,
                rsi14=snap.rsi14,
                macd_hist=snap.macd_hist,
                atr14=_rupees(snap.atr14) if snap.atr14 else None,
                high_52w=_rupees(snap.high_52w) if snap.high_52w else None,
                low_52w=_rupees(snap.low_52w) if snap.low_52w else None,
                from_52w_high=snap.from_52w_high,
                above_200dma=snap.above_200dma,
                trend=snap.trend,
                volatility_pct=technicals.realised_volatility(
                    conn, position.instrument_id, as_of=as_of
                ),
                has_price_history=True,
            )
        peak = _peak_since(conn, position.instrument_id, min(
            (l.lot.buy_date for l in lots), default=as_of), as_of)
        if peak:
            ctx["peak_since_buy"] = _rupees(peak)
            ctx["drawdown_from_peak"] = pct(position.price - peak, peak)
    else:
        x = portfolio_xirr(conn, position.instrument_id, as_of)
        ctx["xirr_pct"] = x * 100 if x is not None else None
        ctx["has_price_history"] = True

    return ctx


def _peak_since(conn: sqlite3.Connection, instrument_id: int, since: str, as_of: str) -> int | None:
    row = conn.execute(
        "SELECT MAX(close) FROM prices WHERE instrument_id=? AND date BETWEEN ? AND ?",
        (instrument_id, since, as_of),
    ).fetchone()
    return row[0] if row and row[0] else None


def instrument_contexts(
    conn: sqlite3.Connection,
    *,
    engine: TaxEngine,
    as_of: str | None = None,
    with_technicals: bool = True,
) -> list[tuple[Position, dict]]:
    as_of = as_of or Date.today().isoformat()
    pos = positions(conn, as_of)
    total = sum(p.market_value for p in pos)
    return [
        (p, instrument_context(conn, p, engine=engine, total_value=total, as_of=as_of,
                               with_technicals=with_technicals))
        for p in pos
    ]


def portfolio_contexts(
    conn: sqlite3.Connection, fy: str, as_of: str | None = None
) -> list[dict]:
    """One context per asset class, for allocation-level rules."""
    as_of = as_of or Date.today().isoformat()
    pos = positions(conn, as_of)
    total = sum(p.market_value for p in pos)
    targets = {
        r["asset_class"]: r["target_pct"]
        for r in conn.execute("SELECT * FROM plan_targets WHERE fy=?", (fy,))
    }

    actual: dict[str, int] = {}
    for p in pos:
        actual[p.asset_class] = actual.get(p.asset_class, 0) + p.market_value

    out = []
    for asset_class in sorted(set(actual) | set(targets)):
        actual_pct = pct(actual.get(asset_class, 0), total)
        target = targets.get(asset_class)
        out.append({
            "name": asset_class,
            "asset_class": asset_class,
            "kind": "PORTFOLIO",
            "total_value": _rupees(total),
            "market_value": _rupees(actual.get(asset_class, 0)),
            "actual_pct": actual_pct,
            "target_pct": target,
            "drift_pct": (actual_pct - target) if target is not None else None,
        })
    return out
