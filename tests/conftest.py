from __future__ import annotations

import sqlite3
from datetime import date as Date, timedelta

import pytest

from fpa.db import connect
from fpa.money import to_paise

TODAY = "2026-08-13"


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = connect(":memory:")
    yield c
    c.close()


@pytest.fixture
def add(conn):
    """Helper returning (instrument, buy, sell) builders for terse test setup."""

    def instrument(name="Test Co", kind="EQUITY", regime="EQUITY", priority=50, symbol="TEST"):
        return conn.execute(
            "INSERT INTO instruments (kind, name, symbol, asset_class, tax_regime, exit_priority)"
            " VALUES (?,?,?,'EQUITY',?,?)",
            (kind, name, symbol, regime, priority),
        ).lastrowid

    def buy(iid, days_ago: int, qty: float, price_rs: float, charges_rs: float = 0):
        d = (Date.fromisoformat(TODAY) - timedelta(days=days_ago)).isoformat()
        p = to_paise(price_rs)
        conn.execute(
            "INSERT INTO transactions (instrument_id, date, kind, quantity, price, amount, charges)"
            " VALUES (?,?,'BUY',?,?,?,?)",
            (iid, d, qty, p, round(qty * p), to_paise(charges_rs)),
        )

    def sell(iid, days_ago: int, qty: float, price_rs: float, charges_rs: float = 0):
        d = (Date.fromisoformat(TODAY) - timedelta(days=days_ago)).isoformat()
        p = to_paise(price_rs)
        conn.execute(
            "INSERT INTO transactions (instrument_id, date, kind, quantity, price, amount, charges)"
            " VALUES (?,?,'SELL',?,?,?,?)",
            (iid, d, qty, p, round(qty * p), to_paise(charges_rs)),
        )

    def price(iid, price_rs: float, date: str = TODAY):
        conn.execute(
            "INSERT OR REPLACE INTO prices (instrument_id, date, close, source)"
            " VALUES (?,?,?,'test')",
            (iid, date, to_paise(price_rs)),
        )

    return type("Add", (), dict(instrument=staticmethod(instrument), buy=staticmethod(buy),
                                sell=staticmethod(sell), price=staticmethod(price)))
