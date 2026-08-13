"""Equity OHLCV from Yahoo Finance. Free, no API key.

NSE symbols take a ``.NS`` suffix, BSE scrip codes take ``.BO``. yfinance is an
unofficial client and does break from time to time — which is exactly why the
ingest boundary is this narrow. Everything downstream reads the ``prices``
table, so swapping in a broker API later touches only this file.
"""

from __future__ import annotations

import sqlite3
from datetime import date as Date, timedelta

from ..money import to_paise

SUFFIX = {"NSE": ".NS", "BSE": ".BO"}


def yahoo_symbol(symbol: str, exchange: str = "NSE") -> str:
    return f"{symbol}{SUFFIX.get(exchange, '.NS')}"


def fetch_history(symbol: str, exchange: str = "NSE", *, period: str = "5y"):
    import yfinance as yf

    ticker = yf.Ticker(yahoo_symbol(symbol, exchange))
    df = ticker.history(period=period, auto_adjust=True)
    if df.empty:
        raise ValueError(f"No price history returned for {yahoo_symbol(symbol, exchange)}")
    return df


def update_prices(
    conn: sqlite3.Connection,
    instrument_ids: list[int] | None = None,
    *,
    period: str = "5y",
) -> dict[str, int]:
    """Refresh price history for tracked equities.

    A failure on one symbol does not abort the rest — a partially fresh table is
    strictly better than a stale one, and the dashboard surfaces staleness per
    instrument rather than trusting the run succeeded.
    """
    sql = "SELECT id, symbol, exchange FROM instruments WHERE kind='EQUITY' AND symbol IS NOT NULL"
    args: list = []
    if instrument_ids:
        sql += f" AND id IN ({','.join('?' * len(instrument_ids))})"
        args = instrument_ids

    stats = {"instruments": 0, "rows": 0, "failed": 0}
    for row in conn.execute(sql, args).fetchall():
        try:
            df = fetch_history(row["symbol"], row["exchange"] or "NSE", period=period)
        except Exception:
            stats["failed"] += 1
            continue

        records = [
            (
                row["id"],
                idx.date().isoformat(),
                to_paise(round(float(r["Open"]), 2)),
                to_paise(round(float(r["High"]), 2)),
                to_paise(round(float(r["Low"]), 2)),
                to_paise(round(float(r["Close"]), 2)),
                int(r["Volume"]) if r["Volume"] == r["Volume"] else None,
                "yfinance",
            )
            for idx, r in df.iterrows()
        ]
        with conn:
            conn.executemany(
                "INSERT OR REPLACE INTO prices (instrument_id, date, open, high, low, close,"
                " volume, source) VALUES (?,?,?,?,?,?,?,?)",
                records,
            )
        stats["instruments"] += 1
        stats["rows"] += len(records)
    return stats


def staleness(conn: sqlite3.Connection, as_of: str | None = None) -> list[dict]:
    """Instruments whose latest price is behind, so the UI can say so plainly.

    Weekends and holidays mean a one- or two-day lag is normal; anything beyond
    a few days usually means an ingest failure worth knowing about.
    """
    as_of_d = Date.fromisoformat(as_of) if as_of else Date.today()
    rows = conn.execute(
        "SELECT i.id, i.name, i.kind, MAX(p.date) AS last_date FROM instruments i"
        " LEFT JOIN prices p ON p.instrument_id = i.id WHERE i.active=1 GROUP BY i.id"
    )
    out = []
    for r in rows:
        last = r["last_date"]
        days = (as_of_d - Date.fromisoformat(last)).days if last else None
        out.append({"id": r["id"], "name": r["name"], "kind": r["kind"],
                    "last_date": last, "days_stale": days})
    return sorted(out, key=lambda x: -(x["days_stale"] if x["days_stale"] is not None else 10**6))


def trading_days_ago(days: int, as_of: str | None = None) -> str:
    d = Date.fromisoformat(as_of) if as_of else Date.today()
    return (d - timedelta(days=days)).isoformat()
