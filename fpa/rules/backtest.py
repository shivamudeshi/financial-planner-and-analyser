"""Replay a rule over history to see whether it was ever worth listening to.

Writing an exit rule takes a minute; knowing whether it helps takes evidence.
This walks a rule backwards through your actual price and transaction history,
records every date it would have fired, and measures what happened next.

**The base rate is the point.** "This rule fired 12 times and the position fell
3% over the next 90 days" sounds like a working sell signal until you notice the
whole market fell 5% over every 90-day window in that period. So every result is
reported against the base rate — the average forward return across *all* dates,
whether the rule fired or not. A rule earns its place by beating that, not by
being directionally right in a falling market.

Two honest limits, both stated in the output rather than buried:

* Your ledger is one portfolio over a few years. A dozen firings is an anecdote,
  not a sample, and the summary says so when n is small.
* Indicators are computed over the full series and then sliced by date. SMA, RSI
  and MACD are causal — the value at date *t* uses only data up to *t* — so this
  introduces no lookahead. Anything non-causal must not be added to the context
  without revisiting this.
"""

from __future__ import annotations

import sqlite3
import statistics
from dataclasses import dataclass, field
from datetime import date as Date, timedelta

import pandas as pd

from ..analysis import technicals
from ..money import pct
from ..tax.engine import TaxEngine, Term
from .engine import Rule
from .evaluator import Expression

DEFAULT_HORIZONS = (30, 90, 180)
SMALL_SAMPLE = 10


@dataclass
class Firing:
    date: str
    subject: str
    instrument_id: int
    price: int
    context: dict
    forward: dict[int, float | None] = field(default_factory=dict)


@dataclass
class BacktestResult:
    rule_name: str
    condition: str
    start: str
    end: str
    step_days: int
    firings: list[Firing] = field(default_factory=list)
    base_rate: dict[int, float | None] = field(default_factory=dict)
    dates_tested: int = 0
    notes: list[str] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.firings)

    def median_forward(self, horizon: int) -> float | None:
        values = [f.forward.get(horizon) for f in self.firings]
        values = [v for v in values if v is not None]
        return statistics.median(values) if values else None

    def hit_rate(self, horizon: int) -> float | None:
        """Share of firings followed by a fall — for an exit rule, a hit."""
        values = [f.forward.get(horizon) for f in self.firings]
        values = [v for v in values if v is not None]
        return (sum(1 for v in values if v < 0) / len(values) * 100) if values else None

    def edge(self, horizon: int) -> float | None:
        """Median forward return after firing, minus the base rate.

        Negative is good for an exit rule: the position did worse after the rule
        fired than it did on an average day.
        """
        med, base = self.median_forward(horizon), self.base_rate.get(horizon)
        return None if med is None or base is None else med - base

    def verdict(self, horizon: int = 90) -> str:
        if self.count == 0:
            return "Never fired over this period. Either the condition is too strict, or the situation never arose."
        edge = self.edge(horizon)
        if edge is None:
            return f"Fired {self.count} time(s), but there is not enough forward history to judge."
        if self.count < SMALL_SAMPLE:
            return (f"Fired only {self.count} time(s) — too few to conclude anything. "
                    f"Edge over base rate was {edge:+.1f}pp at {horizon} days, which is noise "
                    f"at this sample size.")
        if edge < -3:
            return (f"Useful: {self.count} firings, and the position did {abs(edge):.1f}pp worse "
                    f"than the base rate over the next {horizon} days.")
        if edge > 3:
            return (f"Counterproductive: {self.count} firings, and the position did {edge:.1f}pp "
                    f"*better* than average afterwards. This rule sold your winners.")
        return (f"No edge: {self.count} firings, {edge:+.1f}pp against the base rate at "
                f"{horizon} days. The rule is not telling you anything the market wasn't.")


class Backtester:
    def __init__(
        self,
        conn: sqlite3.Connection,
        tax_engine: TaxEngine | None = None,
        *,
        horizons: tuple[int, ...] = DEFAULT_HORIZONS,
    ):
        self.conn = conn
        self.engine = tax_engine or TaxEngine()
        self.horizons = horizons

    def run(
        self,
        rule: Rule,
        *,
        start: str | None = None,
        end: str | None = None,
        step_days: int = 7,
        instrument_ids: list[int] | None = None,
    ) -> BacktestResult:
        end = end or Date.today().isoformat()
        start = start or (Date.fromisoformat(end) - timedelta(days=365 * 3)).isoformat()

        result = BacktestResult(rule_name=rule.name, condition=rule.when.source,
                                start=start, end=end, step_days=step_days)
        if rule.level == "portfolio":
            result.notes.append(
                "Portfolio-level rules are not backtested: allocation targets are a decision "
                "you made recently, not a fact that held historically."
            )
            return result

        instruments = self._instruments(rule, instrument_ids)
        if not instruments:
            result.notes.append("No instruments match this rule's scope.")
            return result

        all_forward: dict[int, list[float]] = {h: [] for h in self.horizons}
        dates = _step_dates(start, end, step_days)
        result.dates_tested = len(dates) * len(instruments)

        for inst in instruments:
            series, indicators = self._prepare(inst["id"], end)
            if series.empty:
                continue
            for d in dates:
                price = _price_at(series, d)
                if price is None:
                    continue

                forward = {h: _forward_return(series, d, h) for h in self.horizons}
                for h, v in forward.items():
                    if v is not None:
                        all_forward[h].append(v)

                ctx = self._context_at(inst, d, price, indicators)
                if not rule.matches(ctx) or not rule.when.evaluate(ctx):
                    continue

                result.firings.append(Firing(
                    date=d, subject=inst["name"], instrument_id=inst["id"], price=price,
                    context={k: _round(ctx.get(k)) for k in sorted(rule.when.names)},
                    forward=forward,
                ))

        for h in self.horizons:
            values = all_forward[h]
            result.base_rate[h] = statistics.median(values) if values else None

        self._notes(result)
        return result

    # -- internals --------------------------------------------------------

    def _instruments(self, rule: Rule, ids: list[int] | None) -> list[sqlite3.Row]:
        rows = self.conn.execute("SELECT * FROM instruments WHERE active=1").fetchall()
        out = []
        for r in rows:
            if ids and r["id"] not in ids:
                continue
            probe = {"kind": r["kind"], "asset_class": r["asset_class"],
                     "category": r["category"], "exit_priority": r["exit_priority"]}
            if all(
                probe.get(k) in (v if isinstance(v, list) else [v])
                for k, v in rule.scope.items()
                if k in probe
            ):
                out.append(r)
        return out

    def _prepare(self, instrument_id: int, end: str):
        """Price series plus causal indicator series, computed once per instrument."""
        df = technicals.price_frame(self.conn, instrument_id, end)
        if df.empty:
            return df, {}
        close = df["close"].astype(float)
        indicators = {
            "sma20": technicals.sma(close, 20),
            "sma50": technicals.sma(close, 50),
            "sma200": technicals.sma(close, 200),
            "rsi14": technicals.rsi(close),
            "macd_hist": technicals.macd(close)["histogram"],
            "high_52w": close.rolling(252, min_periods=20).max(),
            "low_52w": close.rolling(252, min_periods=20).min(),
            "peak": close.cummax(),
        }
        return close, indicators

    def _context_at(self, inst: sqlite3.Row, d: str, price: int, ind: dict) -> dict:
        from ..lots import replay_lots

        lots = replay_lots(self.conn, inst["id"], d)
        qty = sum(l.remaining_qty for l in lots)
        cost = sum(l.cost_remaining for l in lots)
        value = round(qty * price)
        days = [(Date.fromisoformat(d) - Date.fromisoformat(l.buy_date)).days for l in lots]
        to_lt = [self.engine.days_to_long_term(x, None, inst["kind"]) for x in days]

        st = sum(round(l.remaining_qty * price) - l.cost_remaining for l, x in zip(lots, days)
                 if self.engine.term_for(x, None, inst["kind"]) is Term.SHORT)
        lt = (value - cost) - st

        def at(key):
            v = _value_at(ind.get(key), d)
            return v / 100 if v is not None else None

        peak = _value_at(ind.get("peak"), d)
        ctx = {
            "name": inst["name"], "symbol": inst["symbol"] or "", "kind": inst["kind"],
            "category": inst["category"], "asset_class": inst["asset_class"],
            "exit_priority": inst["exit_priority"],
            "quantity": qty, "price": price / 100, "market_value": value / 100,
            "cost": cost / 100, "unrealised": (value - cost) / 100,
            "unrealised_pct": pct(value - cost, cost),
            "weight_pct": None,      # needs whole-portfolio replay; excluded, see notes
            "days_held": max(days) if days else 0,
            "newest_days_held": min(days) if days else 0,
            "short_term_gain": st / 100, "long_term_gain": lt / 100,
            "days_to_long_term": max(to_lt) if to_lt else 0,
            "all_long_term": all(t == 0 for t in to_lt) if to_lt else False,
            "close": price / 100,
            "sma20": at("sma20"), "sma50": at("sma50"), "sma200": at("sma200"),
            "rsi14": _value_at(ind.get("rsi14"), d),
            "macd_hist": _value_at(ind.get("macd_hist"), d),
            "high_52w": at("high_52w"), "low_52w": at("low_52w"),
            "peak_since_buy": peak / 100 if peak else None,
            "atr14": None, "volatility_pct": None, "xirr_pct": None,
            "has_price_history": True,
        }
        hi = _value_at(ind.get("high_52w"), d)
        ctx["from_52w_high"] = pct(price - hi, hi) if hi else None
        ctx["drawdown_from_peak"] = pct(price - peak, peak) if peak else None
        s50, s200 = _value_at(ind.get("sma50"), d), _value_at(ind.get("sma200"), d)
        ctx["above_200dma"] = (price > s200) if s200 else None
        ctx["trend"] = ("Uptrend" if s50 > s200 else "Downtrend") if (s50 and s200) \
            else "Insufficient history"
        return ctx

    def _notes(self, result: BacktestResult) -> None:
        if result.count and result.count < SMALL_SAMPLE:
            result.notes.append(
                f"Only {result.count} firing(s). One portfolio over a few years is an anecdote, "
                f"not a sample — treat this as a sanity check, not evidence."
            )
        result.notes.append(
            "Base rate is the median forward return across every tested date, fired or not. "
            "A rule earns its place by beating that, not by being right in a falling market."
        )
        result.notes.append(
            "`weight_pct` is not reconstructed historically, so rules using it are not "
            "backtestable here and will not fire."
        )


def _step_dates(start: str, end: str, step: int) -> list[str]:
    d, last, out = Date.fromisoformat(start), Date.fromisoformat(end), []
    while d <= last:
        out.append(d.isoformat())
        d += timedelta(days=max(1, step))
    return out


def _value_at(series, d: str):
    if series is None or series.empty:
        return None
    sliced = series[series.index <= pd.Timestamp(d)]
    if sliced.empty:
        return None
    v = sliced.iloc[-1]
    return None if pd.isna(v) else float(v)


def _price_at(series, d: str) -> int | None:
    v = _value_at(series, d)
    return int(v) if v is not None else None


def _forward_return(series, d: str, horizon: int) -> float | None:
    now = _value_at(series, d)
    then_date = (Date.fromisoformat(d) + timedelta(days=horizon)).isoformat()
    if pd.Timestamp(then_date) > series.index.max():
        return None      # not enough future to judge; excluded rather than assumed flat
    then = _value_at(series, then_date)
    if not now or then is None:
        return None
    return (then - now) / now * 100


def _round(v):
    return round(v, 4) if isinstance(v, float) else v
