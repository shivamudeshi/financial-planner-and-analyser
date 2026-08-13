"""Broker tradebook CSV import for the equity side.

A CAS covers mutual funds only, so equity transactions come from your broker's
tradebook export. Every major Indian broker offers one, and they all carry the
same facts under different column names — so rather than a parser per broker,
this maps aliases onto a canonical schema and fails loudly when a required
column is missing.

Charges are usually *not* in the tradebook (they live in the contract note), so
they are estimated from `costs.yaml` unless the file provides them. That
estimate feeds cost basis, so it is stated rather than hidden: the import result
says how many rows used estimated charges.
"""

from __future__ import annotations

import csv
import hashlib
import io
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from ..money import to_paise

# Canonical field -> the column names brokers actually use.
ALIASES: dict[str, tuple[str, ...]] = {
    "date": ("trade_date", "date", "order_execution_time", "trade date", "transaction date"),
    "symbol": ("symbol", "tradingsymbol", "scrip", "stock", "instrument", "security"),
    "isin": ("isin", "isin code"),
    "side": ("trade_type", "type", "buy/sell", "transaction_type", "side", "action"),
    "quantity": ("quantity", "qty", "no. of shares", "shares"),
    "price": ("price", "trade_price", "rate", "avg_price", "average price"),
    "exchange": ("exchange", "exch"),
    "charges": ("charges", "brokerage", "total_charges", "taxes and charges"),
    "trade_id": ("trade_id", "tradeid", "order_id", "trade no", "reference"),
}

BUY_WORDS = {"buy", "b", "purchase", "bought"}
SELL_WORDS = {"sell", "s", "sale", "sold"}

DATE_FORMATS = (
    "%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%d-%b-%Y", "%d %b %Y",
    "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%d-%m-%Y %H:%M:%S",
)


@dataclass
class Trade:
    date: str
    symbol: str
    side: str          # 'BUY' | 'SELL'
    quantity: float
    price: int         # paise
    exchange: str = "NSE"
    isin: str | None = None
    charges: int | None = None   # None means "estimate it"
    trade_id: str | None = None

    @property
    def amount(self) -> int:
        return round(self.quantity * self.price)


@dataclass
class TradebookResult:
    trades: list[Trade] = field(default_factory=list)
    instruments_created: int = 0
    transactions_added: int = 0
    duplicates_skipped: int = 0
    charges_estimated: int = 0
    warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _normalise(header: str) -> str:
    return header.strip().lower().replace("_", " ").replace("-", " ")


def _map_columns(fieldnames: list[str]) -> dict[str, str]:
    """Resolve a broker's headers onto canonical names."""
    lookup = {_normalise(f): f for f in fieldnames}
    mapping: dict[str, str] = {}
    for canonical, aliases in ALIASES.items():
        for alias in aliases:
            key = _normalise(alias)
            if key in lookup:
                mapping[canonical] = lookup[key]
                break
    return mapping


def _parse_date(raw: str) -> str:
    raw = raw.strip()
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(raw[: len(fmt) + 6], fmt).date().isoformat()
        except ValueError:
            continue
    # Last resort: a leading ISO date inside a longer timestamp.
    try:
        return datetime.fromisoformat(raw.replace("Z", "")).date().isoformat()
    except ValueError as exc:
        raise ValueError(f"Unrecognised date format: {raw!r}") from exc


def _side(raw: str) -> str | None:
    v = raw.strip().lower()
    if v in BUY_WORDS:
        return "BUY"
    if v in SELL_WORDS:
        return "SELL"
    return None


def parse_csv(text: str) -> TradebookResult:
    """Parse a tradebook export into canonical trades."""
    result = TradebookResult()
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        result.warnings.append("Empty file — no header row found.")
        return result

    cols = _map_columns(list(reader.fieldnames))
    missing = [f for f in ("date", "symbol", "side", "quantity", "price") if f not in cols]
    if missing:
        result.warnings.append(
            f"Missing required column(s): {', '.join(missing)}. Found: "
            f"{', '.join(reader.fieldnames)}. Add the column or rename it to one of the "
            f"aliases in ingest/tradebook.py."
        )
        return result

    for n, row in enumerate(reader, start=2):
        raw_side = (row.get(cols["side"]) or "").strip()
        side = _side(raw_side)
        if side is None:
            result.warnings.append(f"Row {n}: unrecognised trade type {raw_side!r}, skipped.")
            continue
        try:
            qty = float((row[cols["quantity"]] or "0").replace(",", ""))
            price = float((row[cols["price"]] or "0").replace(",", ""))
            date = _parse_date(row[cols["date"]])
        except (ValueError, KeyError) as exc:
            result.warnings.append(f"Row {n}: {exc}, skipped.")
            continue
        if qty <= 0 or price <= 0:
            result.warnings.append(f"Row {n}: non-positive quantity or price, skipped.")
            continue

        charges = None
        if "charges" in cols and (raw := (row.get(cols["charges"]) or "").strip()):
            try:
                charges = to_paise(abs(float(raw.replace(",", ""))))
            except ValueError:
                pass

        result.trades.append(
            Trade(
                date=date,
                symbol=(row[cols["symbol"]] or "").strip().upper(),
                side=side,
                quantity=qty,
                price=to_paise(price),
                exchange=((row.get(cols.get("exchange", ""), "") or "NSE").strip().upper()[:3]
                          or "NSE"),
                isin=((row.get(cols.get("isin", ""), "") or "").strip() or None),
                charges=charges,
                trade_id=((row.get(cols.get("trade_id", ""), "") or "").strip() or None),
            )
        )

    result.trades.sort(key=lambda t: (t.date, t.symbol))
    return result


def parse_file(path: Path | str) -> TradebookResult:
    return parse_csv(Path(path).read_text(encoding="utf-8-sig"))


def external_id(trade: Trade) -> str:
    raw = (trade.trade_id or
           f"{trade.date}|{trade.symbol}|{trade.side}|{trade.quantity}|{trade.price}")
    return "tb-" + hashlib.sha1(raw.encode()).hexdigest()[:16]


def import_trades(
    conn: sqlite3.Connection,
    result: TradebookResult,
    *,
    dry_run: bool = False,
    estimate_charges: bool = True,
) -> TradebookResult:
    """Write trades into the ledger, creating instruments as needed."""
    analyser = None
    if estimate_charges:
        from ..planner.opportunity import OpportunityAnalyser

        analyser = OpportunityAnalyser(conn)

    for trade in result.trades:
        instrument_id, created = _resolve_instrument(conn, trade, dry_run=dry_run)
        result.instruments_created += created

        ext = external_id(trade)
        if conn.execute("SELECT 1 FROM transactions WHERE external_id=?", (ext,)).fetchone():
            result.duplicates_skipped += 1
            continue

        charges = trade.charges
        if charges is None and analyser is not None:
            costs = (analyser.buy_costs(trade.amount, "EQUITY") if trade.side == "BUY"
                     else analyser.sell_costs(trade.amount, "EQUITY"))
            charges = costs.total
            result.charges_estimated += 1
        charges = charges or 0

        if dry_run or instrument_id is None:
            result.transactions_added += 1
            continue

        conn.execute(
            "INSERT INTO transactions (instrument_id, date, kind, quantity, price, amount,"
            " charges, account, external_id) VALUES (?,?,?,?,?,?,?,?,?)",
            (instrument_id, trade.date, trade.side, trade.quantity, trade.price,
             trade.amount, charges, "tradebook", ext),
        )
        result.transactions_added += 1

    if result.charges_estimated:
        result.notes.append(
            f"{result.charges_estimated} trade(s) had no charges column, so brokerage, STT, "
            f"stamp duty and GST were estimated from planner/costs.yaml. That estimate feeds "
            f"cost basis — check the rates there match your broker."
        )
    if dry_run:
        result.notes.append("Dry run — nothing was written.")
    else:
        conn.commit()
    return result


def _resolve_instrument(
    conn: sqlite3.Connection, trade: Trade, *, dry_run: bool
) -> tuple[int | None, int]:
    for column, value in (("isin", trade.isin), ("symbol", trade.symbol)):
        if not value:
            continue
        row = conn.execute(
            f"SELECT id FROM instruments WHERE {column}=? AND kind='EQUITY'", (value,)
        ).fetchone()
        if row:
            return row["id"], 0

    if dry_run:
        return None, 1
    iid = conn.execute(
        "INSERT INTO instruments (kind, name, symbol, isin, exchange, asset_class, tax_regime)"
        " VALUES ('EQUITY',?,?,?,?,'EQUITY','EQUITY')",
        (trade.symbol, trade.symbol, trade.isin, trade.exchange),
    ).lastrowid
    return iid, 1
