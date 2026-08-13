"""Current positions: open lots valued at the latest known price."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date as Date

from .lots import OpenLot, open_lots
from .money import pct
from .tax.engine import Regime, TaxEngine, Term


@dataclass
class LotView:
    """An open lot with everything the planner needs to reason about selling it."""

    lot: OpenLot
    instrument_id: int
    name: str
    symbol: str
    kind: str
    regime: Regime
    exit_priority: int
    price: int
    as_of: str

    @property
    def quantity(self) -> float:
        return self.lot.remaining_qty

    @property
    def market_value(self) -> int:
        return round(self.quantity * self.price)

    @property
    def cost(self) -> int:
        return self.lot.cost_remaining

    @property
    def gain(self) -> int:
        return self.market_value - self.cost

    @property
    def gain_pct(self) -> float:
        return pct(self.gain, self.cost)

    @property
    def days_held(self) -> int:
        return (Date.fromisoformat(self.as_of) - Date.fromisoformat(self.lot.buy_date)).days

    def term(self, engine: TaxEngine) -> Term:
        return engine.term_for(self.days_held, self.regime, self.kind)

    def days_to_long_term(self, engine: TaxEngine) -> int:
        return engine.days_to_long_term(self.days_held, self.regime, self.kind)

    def long_term_date(self, engine: TaxEngine) -> str:
        d = engine.days_to_long_term(self.days_held, self.regime, self.kind)
        return (Date.fromisoformat(self.as_of).toordinal() + d) and Date.fromordinal(
            Date.fromisoformat(self.as_of).toordinal() + d
        ).isoformat()


@dataclass
class Position:
    """All open lots of one instrument, aggregated."""

    instrument_id: int
    name: str
    symbol: str
    kind: str
    category: str
    asset_class: str
    exit_priority: int
    price: int
    lots: list[LotView]

    @property
    def quantity(self) -> float:
        return sum(l.quantity for l in self.lots)

    @property
    def market_value(self) -> int:
        return sum(l.market_value for l in self.lots)

    @property
    def cost(self) -> int:
        return sum(l.cost for l in self.lots)

    @property
    def gain(self) -> int:
        return self.market_value - self.cost

    @property
    def gain_pct(self) -> float:
        return pct(self.gain, self.cost)

    def unrealised_by_term(self, engine: TaxEngine) -> dict[Term, int]:
        out = {Term.SHORT: 0, Term.LONG: 0}
        for l in self.lots:
            out[l.term(engine)] += l.gain
        return out


def latest_prices(conn: sqlite3.Connection, as_of: str | None = None) -> dict[int, tuple[int, str]]:
    """Most recent close per instrument at or before ``as_of``."""
    clause = "WHERE date <= ?" if as_of else ""
    args = (as_of,) if as_of else ()
    rows = conn.execute(
        f"""
        SELECT p.instrument_id, p.close, p.date FROM prices p
        JOIN (SELECT instrument_id, MAX(date) AS d FROM prices {clause}
              GROUP BY instrument_id) m
          ON m.instrument_id = p.instrument_id AND m.d = p.date
        """,
        args,
    )
    return {r["instrument_id"]: (r["close"], r["date"]) for r in rows}


def lot_views(conn: sqlite3.Connection, as_of: str | None = None) -> list[LotView]:
    as_of = as_of or Date.today().isoformat()
    prices = latest_prices(conn, as_of)
    meta = {
        r["id"]: r
        for r in conn.execute(
            "SELECT id, name, symbol, kind, tax_regime, exit_priority FROM instruments"
        )
    }
    views = []
    for lot in open_lots(conn):
        m = meta[lot.instrument_id]
        price, _ = prices.get(lot.instrument_id, (lot.cost_per_unit, as_of))
        views.append(
            LotView(
                lot=lot,
                instrument_id=lot.instrument_id,
                name=m["name"],
                symbol=m["symbol"] or "",
                kind=m["kind"],
                regime=Regime(m["tax_regime"]),
                exit_priority=m["exit_priority"],
                price=price,
                as_of=as_of,
            )
        )
    return views


def positions(conn: sqlite3.Connection, as_of: str | None = None) -> list[Position]:
    as_of = as_of or Date.today().isoformat()
    meta = {r["id"]: r for r in conn.execute("SELECT * FROM instruments")}
    grouped: dict[int, list[LotView]] = {}
    for v in lot_views(conn, as_of):
        grouped.setdefault(v.instrument_id, []).append(v)

    out = []
    for iid, lots in grouped.items():
        m = meta[iid]
        out.append(
            Position(
                instrument_id=iid,
                name=m["name"],
                symbol=m["symbol"] or "",
                kind=m["kind"],
                category=m["category"] or "",
                asset_class=m["asset_class"],
                exit_priority=m["exit_priority"],
                price=lots[0].price,
                lots=lots,
            )
        )
    return sorted(out, key=lambda p: -p.market_value)
