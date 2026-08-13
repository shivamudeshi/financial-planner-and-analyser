"""Generates a plausible Indian portfolio so the app is explorable before you
import anything real.

Deliberately constructed to exercise the planner's interesting cases: lots just
short of the 12-month line, long-term winners far past it, a loss-making
position, gains already realised earlier in the year, and a brought-forward
loss. Prices are a seeded random walk — realistic in shape, not real data.
"""

from __future__ import annotations

import random
import sqlite3
from datetime import date as Date, timedelta

from .db import financial_year
from .money import to_paise

SEED = 20260813

# (symbol, name, category, start_price, annual_drift, annual_vol, exit_priority)
EQUITIES = [
    ("RELIANCE",   "Reliance Industries",      "Energy",      2450, 0.14, 0.24, 20),
    ("HDFCBANK",   "HDFC Bank",                "Financials",  1520, 0.11, 0.20, 15),
    ("INFY",       "Infosys",                  "IT",          1380, 0.06, 0.26, 70),
    ("TCS",        "Tata Consultancy Services","IT",          3550, 0.05, 0.22, 45),
    ("ITC",        "ITC",                      "FMCG",         420, 0.13, 0.19, 30),
    ("TATAMOTORS", "Tata Motors",              "Auto",         620, 0.28, 0.38, 85),
    ("ZOMATO",     "Eternal (Zomato)",         "Consumer",     135, 0.35, 0.52, 90),
    ("SBIN",       "State Bank of India",      "Financials",   590, 0.16, 0.28, 25),
    ("ASIANPAINT", "Asian Paints",             "Materials",   3100, -0.12, 0.24, 80),
    ("PIDILITIND", "Pidilite Industries",      "Materials",   2680, 0.09, 0.21, 35),
    ("DMART",      "Avenue Supermarts",        "Retail",      3900, -0.08, 0.27, 75),
    ("BAJFINANCE", "Bajaj Finance",            "Financials",  6800, 0.10, 0.30, 40),
]

# (amfi_code, name, category, start_nav, drift, vol, exit_priority, regime)
FUNDS = [
    ("120503", "Parag Parikh Flexi Cap - Direct Growth",   "Flexi Cap",  58.0, 0.16, 0.15, 10, "EQUITY"),
    ("119551", "Mirae Asset Large Cap - Direct Growth",    "Large Cap",  92.0, 0.11, 0.14, 55, "EQUITY"),
    ("125497", "Quant Small Cap - Direct Growth",          "Small Cap",  185.0, 0.22, 0.29, 65, "EQUITY"),
    ("118989", "HDFC Mid-Cap Opportunities - Direct",      "Mid Cap",    142.0, 0.18, 0.22, 30, "EQUITY"),
    ("120716", "UTI Nifty 50 Index - Direct Growth",       "Index",      145.0, 0.12, 0.13, 15, "EQUITY"),
    ("119060", "ICICI Pru Corporate Bond - Direct Growth", "Debt",        28.0, 0.07, 0.02, 50, "OTHER"),
]


def _walk(rng: random.Random, start: float, drift: float, vol: float, days: int) -> list[float]:
    """Daily geometric random walk on ~250 trading days a year."""
    px, out = start, []
    dt = 1 / 250
    for _ in range(days):
        shock = rng.gauss(0, 1) * vol * (dt**0.5)
        px = max(0.5, px * (1 + drift * dt + shock))
        out.append(px)
    return out


def generate(conn: sqlite3.Connection, as_of: str | None = None, years: int = 3) -> dict[str, int]:
    """Populate an empty DB. Safe to re-run: clears prior sample rows first."""
    as_of_d = Date.fromisoformat(as_of) if as_of else Date.today()
    rng = random.Random(SEED)
    start = as_of_d - timedelta(days=365 * years)
    n_days = (as_of_d - start).days + 1
    dates = [(start + timedelta(days=i)).isoformat() for i in range(n_days)]
    # Skip weekends so the series looks like a real trading calendar.
    trading = [d for d in dates if Date.fromisoformat(d).weekday() < 5]

    with conn:
        for t in ("disposals", "lots", "transactions", "prices", "instruments",
                  "carry_forward_losses", "plan_targets"):
            conn.execute(f"DELETE FROM {t}")

    stats = {"instruments": 0, "prices": 0, "transactions": 0}

    with conn:
        for sym, name, cat, px0, drift, vol, prio in EQUITIES:
            iid = conn.execute(
                "INSERT INTO instruments (kind, name, symbol, exchange, category, asset_class,"
                " tax_regime, exit_priority, isin) VALUES ('EQUITY',?,?,'NSE',?,'EQUITY','EQUITY',?,?)",
                (name, sym, cat, prio, f"INE{abs(hash(sym)) % 10**9:09d}"),
            ).lastrowid
            series = _walk(rng, px0, drift, vol, len(trading))
            stats["prices"] += _write_prices(conn, iid, trading, series)
            stats["transactions"] += _equity_txns(conn, rng, iid, trading, series, sym)
            stats["instruments"] += 1

        for code, name, cat, nav0, drift, vol, prio, regime in FUNDS:
            asset = "DEBT" if regime == "OTHER" else "EQUITY"
            iid = conn.execute(
                "INSERT INTO instruments (kind, name, amfi_code, category, asset_class,"
                " tax_regime, exit_priority) VALUES ('MF',?,?,?,?,?,?)",
                (name, code, cat, asset, regime, prio),
            ).lastrowid
            series = _walk(rng, nav0, drift, vol, len(trading))
            stats["prices"] += _write_prices(conn, iid, trading, series, mf=True)
            stats["transactions"] += _sip_txns(conn, iid, trading, series, code)
            stats["instruments"] += 1

        # A loss carried in from before the ledger starts.
        prior_fy = financial_year((as_of_d - timedelta(days=400)).isoformat())
        conn.execute(
            "INSERT INTO carry_forward_losses (fy, term, amount) VALUES (?,'SHORT_TERM',?)",
            (prior_fy, to_paise(18_000)),
        )

        for asset, target in (("EQUITY", 75.0), ("DEBT", 20.0), ("GOLD", 5.0)):
            conn.execute(
                "INSERT INTO plan_targets (fy, asset_class, target_pct) VALUES (?,?,?)",
                (financial_year(as_of_d.isoformat()), asset, target),
            )

    return stats


def _write_prices(conn, iid: int, dates: list[str], series: list[float], mf: bool = False) -> int:
    rows = []
    for d, px in zip(dates, series):
        p = to_paise(round(px, 2))
        if mf:
            rows.append((iid, d, None, None, None, p, None, "sample"))
        else:
            rows.append((iid, d, p, round(p * 1.008), round(p * 0.992), p,
                         100_000 + (hash(d) % 900_000), "sample"))
    conn.executemany(
        "INSERT OR REPLACE INTO prices (instrument_id, date, open, high, low, close, volume, source)"
        " VALUES (?,?,?,?,?,?,?,?)",
        rows,
    )
    return len(rows)


def _equity_txns(conn, rng, iid: int, dates: list[str], series: list[float], sym: str) -> int:
    """Two or three buys spread over the history, plus an occasional part-sale."""
    n = len(dates)
    picks = sorted(rng.sample(range(0, int(n * 0.92)), rng.choice([2, 3])))
    count = 0
    for k, idx in enumerate(picks):
        qty = rng.choice([10, 15, 25, 40, 50, 75])
        price = to_paise(round(series[idx], 2))
        amount = qty * price
        conn.execute(
            "INSERT INTO transactions (instrument_id, date, kind, quantity, price, amount,"
            " charges, account, external_id) VALUES (?,?,'BUY',?,?,?,?,'sample-broker',?)",
            (iid, dates[idx], qty, price, amount, round(amount * 0.0012), f"{sym}-B{k}"),
        )
        count += 1

    # One position gets a part-sale earlier in the current FY, so the planner has
    # to work around gains already booked.
    if sym in ("ITC", "SBIN"):
        idx = int(n * 0.96)
        qty = 10
        price = to_paise(round(series[idx], 2))
        amount = qty * price
        conn.execute(
            "INSERT INTO transactions (instrument_id, date, kind, quantity, price, amount,"
            " charges, account, external_id) VALUES (?,?,'SELL',?,?,?,?,'sample-broker',?)",
            (iid, dates[idx], qty, price, amount, round(amount * 0.0012), f"{sym}-S0"),
        )
        count += 1
    return count


def _sip_txns(conn, iid: int, dates: list[str], series: list[float], code: str) -> int:
    """Monthly SIP on the first trading day of each month."""
    seen, count = set(), 0
    amount = to_paise(5_000)
    for d, nav in zip(dates, series):
        ym = d[:7]
        if ym in seen:
            continue
        seen.add(ym)
        nav_p = to_paise(round(nav, 4))
        units = round(amount / nav_p, 4)
        conn.execute(
            "INSERT INTO transactions (instrument_id, date, kind, quantity, price, amount,"
            " charges, account, external_id) VALUES (?,?,'SIP',?,?,?,0,'sample-folio',?)",
            (iid, d, units, nav_p, amount, f"{code}-{ym}"),
        )
        count += 1
    return count
