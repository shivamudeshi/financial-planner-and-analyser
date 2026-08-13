"""FIFO tax-lot construction.

Indian tax requires FIFO matching for listed securities, and the short/long
split drives real money, so lots are first-class rather than inferred at report
time. ``rebuild`` is destructive and total: derived state is always a pure
function of ``transactions``.

Cost basis includes buy-side charges (brokerage, stamp duty, GST). Sale
consideration is reduced by sell-side brokerage, which is an allowable transfer
expense. STT is deliberately *not* deducted — it is not allowable against
capital gains for STT-paid equity.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date as Date
from typing import Iterator

from .db import financial_year
from .tax.engine import Gain, Regime, TaxEngine, Term

# Fractional MF units mean float quantities; anything under this is settlement dust.
QTY_EPS = 1e-6


@dataclass
class OpenLot:
    id: int
    instrument_id: int
    buy_date: str
    quantity: float
    remaining_qty: float
    cost_per_unit: int
    fmv_2018: int | None = None

    @property
    def cost_remaining(self) -> int:
        return round(self.remaining_qty * self.cost_per_unit)

    def days_held(self, as_of: str) -> int:
        return (Date.fromisoformat(as_of) - Date.fromisoformat(self.buy_date)).days


def _parse(d: str) -> Date:
    return Date.fromisoformat(d)


def rebuild(conn: sqlite3.Connection, engine: TaxEngine | None = None) -> dict[str, int]:
    """Recompute every lot and disposal from ``transactions``.

    Returns counts for the caller to log. Runs in one transaction so a mid-way
    failure leaves the previous derived state intact.
    """
    engine = engine or TaxEngine()
    stats = {"lots": 0, "disposals": 0, "unmatched_sells": 0}

    with conn:
        conn.execute("DELETE FROM disposals")
        conn.execute("DELETE FROM lots")

        instruments = conn.execute(
            "SELECT id, kind, tax_regime FROM instruments ORDER BY id"
        ).fetchall()

        for inst in instruments:
            regime = Regime(inst["tax_regime"])
            open_lots: list[OpenLot] = []

            for txn in _transactions_for(conn, inst["id"]):
                kind = txn["kind"]

                if kind in ("BUY", "SIP", "SWITCH_IN"):
                    qty = txn["quantity"]
                    if qty <= QTY_EPS:
                        continue
                    total_cost = abs(txn["amount"]) + txn["charges"]
                    lot_id = conn.execute(
                        "INSERT INTO lots (instrument_id, buy_txn_id, buy_date, quantity,"
                        " remaining_qty, cost_per_unit) VALUES (?,?,?,?,?,?)",
                        (inst["id"], txn["id"], txn["date"], qty, qty, round(total_cost / qty)),
                    ).lastrowid
                    open_lots.append(
                        OpenLot(lot_id, inst["id"], txn["date"], qty, qty, round(total_cost / qty))
                    )
                    stats["lots"] += 1

                elif kind == "BONUS":
                    # Zero-cost units. Holding period runs from the allotment date,
                    # so they are a fresh lot rather than an adjustment to the parent.
                    qty = txn["quantity"]
                    if qty <= QTY_EPS:
                        continue
                    lot_id = conn.execute(
                        "INSERT INTO lots (instrument_id, buy_txn_id, buy_date, quantity,"
                        " remaining_qty, cost_per_unit) VALUES (?,?,?,?,?,0)",
                        (inst["id"], txn["id"], txn["date"], qty, qty),
                    ).lastrowid
                    open_lots.append(OpenLot(lot_id, inst["id"], txn["date"], qty, qty, 0))
                    stats["lots"] += 1

                elif kind == "SPLIT":
                    # quantity carries the ratio: 5 means one share becomes five.
                    ratio = txn["quantity"]
                    if ratio <= 0:
                        continue
                    for lot in open_lots:
                        lot.quantity *= ratio
                        lot.remaining_qty *= ratio
                        lot.cost_per_unit = round(lot.cost_per_unit / ratio)
                        conn.execute(
                            "UPDATE lots SET quantity=?, remaining_qty=?, cost_per_unit=?"
                            " WHERE id=?",
                            (lot.quantity, lot.remaining_qty, lot.cost_per_unit, lot.id),
                        )

                elif kind in ("SELL", "SWITCH_OUT"):
                    stats["disposals"] += _consume(
                        conn, engine, inst, regime, txn, open_lots, stats
                    )

                # DIVIDEND affects income, not capital gains — no lot impact.

    return stats


def _transactions_for(conn: sqlite3.Connection, instrument_id: int) -> Iterator[sqlite3.Row]:
    yield from conn.execute(
        "SELECT * FROM transactions WHERE instrument_id=? ORDER BY date, id",
        (instrument_id,),
    )


def _consume(
    conn: sqlite3.Connection,
    engine: TaxEngine,
    inst: sqlite3.Row,
    regime: Regime,
    txn: sqlite3.Row,
    open_lots: list[OpenLot],
    stats: dict[str, int],
) -> int:
    """Match a sale against open lots FIFO, writing one disposal per lot touched."""
    to_sell = txn["quantity"]
    gross = abs(txn["amount"])
    # Net consideration after allowable transfer expenses.
    net_proceeds = gross - txn["charges"]
    unit_price = net_proceeds / to_sell if to_sell > QTY_EPS else 0
    made = 0

    for lot in open_lots:
        if to_sell <= QTY_EPS:
            break
        if lot.remaining_qty <= QTY_EPS:
            continue

        qty = min(lot.remaining_qty, to_sell)
        sale_value = round(qty * unit_price)
        cost = round(qty * lot.cost_per_unit)
        if lot.fmv_2018 is not None:
            cost = engine.grandfathered_cost(cost, round(qty * lot.fmv_2018), sale_value)

        days = (_parse(txn["date"]) - _parse(lot.buy_date)).days
        term = engine.term_for(days, regime, inst["kind"])

        conn.execute(
            "INSERT INTO disposals (lot_id, sell_txn_id, instrument_id, sell_date, quantity,"
            " sale_value, cost, gain, days_held, term, fy) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                lot.id, txn["id"], inst["id"], txn["date"], qty, sale_value, cost,
                sale_value - cost, days, term.value, financial_year(txn["date"]),
            ),
        )
        lot.remaining_qty -= qty
        conn.execute(
            "UPDATE lots SET remaining_qty=? WHERE id=?", (lot.remaining_qty, lot.id)
        )
        to_sell -= qty
        made += 1

    if to_sell > QTY_EPS:
        # Selling more than the ledger knows you hold: almost always a missing
        # buy import. Surfaced rather than silently absorbed.
        stats["unmatched_sells"] += 1
    return made


def replay_lots(
    conn: sqlite3.Connection, instrument_id: int, as_of: str
) -> list[OpenLot]:
    """Reconstruct open lots as they stood on ``as_of``, without touching the DB.

    The ``lots`` table holds *current* remaining quantities, so it cannot answer
    "what did I hold last March?" — which is exactly what a backtest needs.
    This replays the same FIFO logic over transactions up to a date and returns
    the result in memory.
    """
    open_: list[OpenLot] = []
    rows = conn.execute(
        "SELECT * FROM transactions WHERE instrument_id=? AND date <= ? ORDER BY date, id",
        (instrument_id, as_of),
    )
    for txn in rows:
        kind, qty = txn["kind"], txn["quantity"]

        if kind in ("BUY", "SIP", "SWITCH_IN", "BONUS"):
            if qty <= QTY_EPS:
                continue
            cost = 0 if kind == "BONUS" else abs(txn["amount"]) + txn["charges"]
            open_.append(OpenLot(txn["id"], instrument_id, txn["date"], qty, qty,
                                 round(cost / qty)))

        elif kind == "SPLIT" and qty > 0:
            for lot in open_:
                lot.quantity *= qty
                lot.remaining_qty *= qty
                lot.cost_per_unit = round(lot.cost_per_unit / qty)

        elif kind in ("SELL", "SWITCH_OUT"):
            to_sell = qty
            for lot in open_:
                if to_sell <= QTY_EPS:
                    break
                take = min(lot.remaining_qty, to_sell)
                lot.remaining_qty -= take
                to_sell -= take

    return [l for l in open_ if l.remaining_qty > QTY_EPS]


def open_lots(conn: sqlite3.Connection, instrument_id: int | None = None) -> list[OpenLot]:
    sql = "SELECT * FROM lots WHERE remaining_qty > ?"
    args: list = [QTY_EPS]
    if instrument_id is not None:
        sql += " AND instrument_id=?"
        args.append(instrument_id)
    sql += " ORDER BY instrument_id, buy_date, id"
    return [
        OpenLot(
            r["id"], r["instrument_id"], r["buy_date"], r["quantity"],
            r["remaining_qty"], r["cost_per_unit"], r["fmv_2018"],
        )
        for r in conn.execute(sql, args)
    ]


def realised_gains(conn: sqlite3.Connection, fy: str) -> list[Gain]:
    """Gains already booked in ``fy``, as tax-engine inputs."""
    rows = conn.execute(
        "SELECT d.gain, d.term, i.tax_regime, i.name FROM disposals d"
        " JOIN instruments i ON i.id = d.instrument_id WHERE d.fy = ?",
        (fy,),
    )
    return [
        Gain(r["gain"], Term(r["term"]), Regime(r["tax_regime"]), r["name"]) for r in rows
    ]


def brought_forward(conn: sqlite3.Connection, fy: str) -> tuple[int, int]:
    """Unconsumed STCL and LTCL from years before ``fy``."""
    rows = conn.execute(
        "SELECT term, SUM(amount - consumed) AS net FROM carry_forward_losses"
        " WHERE fy < ? GROUP BY term",
        (fy,),
    )
    out = {r["term"]: max(0, r["net"] or 0) for r in rows}
    return out.get("SHORT_TERM", 0), out.get("LONG_TERM", 0)
