from __future__ import annotations

import pytest

from fpa.ingest import tradebook
from fpa.money import to_paise

L = to_paise

# Zerodha-style export.
ZERODHA = """symbol,isin,trade_date,exchange,segment,trade_type,quantity,price,trade_id
RELIANCE,INE002A01018,2025-06-10,NSE,EQ,buy,25,2450.50,10001
INFY,INE009A01021,2025-07-15,NSE,EQ,buy,50,1380.25,10002
RELIANCE,INE002A01018,2026-02-20,NSE,EQ,sell,10,2900.00,10003
"""

# Different broker: different headers, different date format, charges included.
OTHER_BROKER = """Scrip,Transaction Date,Type,Qty,Rate,Exchange,Total Charges
TCS,10-06-2025,B,15,3550.00,NSE,42.50
TCS,20-01-2026,S,5,4100.00,NSE,31.20
"""

MESSY = """symbol,trade_date,trade_type,quantity,price
GOODCO,2025-06-10,buy,10,100.00
BADSIDE,2025-06-11,transfer,10,100.00
BADQTY,2025-06-12,buy,0,100.00
BADDATE,not-a-date,buy,10,100.00
"""


class TestColumnMapping:
    def test_parses_a_zerodha_export(self):
        r = tradebook.parse_csv(ZERODHA)
        assert len(r.trades) == 3
        assert r.warnings == []
        t = r.trades[0]
        assert (t.symbol, t.side, t.quantity, t.price) == ("RELIANCE", "BUY", 25.0, L(2450.50))
        assert t.isin == "INE002A01018"

    def test_parses_a_differently_headed_export(self):
        r = tradebook.parse_csv(OTHER_BROKER)
        assert len(r.trades) == 2
        assert r.trades[0].symbol == "TCS"
        assert r.trades[0].date == "2025-06-10"     # DD-MM-YYYY understood
        assert r.trades[1].side == "SELL"           # 'S'

    def test_uses_charges_from_the_file_when_present(self):
        r = tradebook.parse_csv(OTHER_BROKER)
        assert r.trades[0].charges == L(42.50)

    def test_missing_charges_column_leaves_it_unset(self):
        assert tradebook.parse_csv(ZERODHA).trades[0].charges is None

    def test_missing_required_column_fails_loudly(self):
        r = tradebook.parse_csv("symbol,quantity\nRELIANCE,10\n")
        assert r.trades == []
        assert any("Missing required column" in w for w in r.warnings)
        assert any("price" in w for w in r.warnings)

    def test_trades_are_sorted_by_date(self):
        r = tradebook.parse_csv(ZERODHA)
        assert [t.date for t in r.trades] == sorted(t.date for t in r.trades)


class TestBadRows:
    def test_bad_rows_are_skipped_and_reported_individually(self):
        r = tradebook.parse_csv(MESSY)
        assert [t.symbol for t in r.trades] == ["GOODCO"]
        assert len(r.warnings) == 3
        assert any("transfer" in w for w in r.warnings)
        assert any("non-positive" in w for w in r.warnings)
        assert any("date format" in w for w in r.warnings)

    def test_empty_file_is_reported(self):
        assert any("Empty file" in w for w in tradebook.parse_csv("").warnings)


class TestImport:
    def test_creates_instruments_and_transactions(self, conn):
        r = tradebook.import_trades(conn, tradebook.parse_csv(ZERODHA))
        assert r.instruments_created == 2          # RELIANCE and INFY
        assert r.transactions_added == 3
        assert conn.execute(
            "SELECT COUNT(*) FROM instruments WHERE kind='EQUITY'").fetchone()[0] == 2

    def test_reimport_is_idempotent(self, conn):
        tradebook.import_trades(conn, tradebook.parse_csv(ZERODHA))
        second = tradebook.import_trades(conn, tradebook.parse_csv(ZERODHA))
        assert second.transactions_added == 0
        assert second.duplicates_skipped == 3

    def test_estimated_charges_are_applied_and_disclosed(self, conn):
        r = tradebook.import_trades(conn, tradebook.parse_csv(ZERODHA))
        assert r.charges_estimated == 3
        assert any("estimated from planner/costs.yaml" in n for n in r.notes)
        charges = conn.execute(
            "SELECT charges FROM transactions ORDER BY date").fetchall()
        assert all(c[0] > 0 for c in charges)

    def test_charges_from_file_are_not_overwritten(self, conn):
        r = tradebook.import_trades(conn, tradebook.parse_csv(OTHER_BROKER))
        assert r.charges_estimated == 0
        assert conn.execute(
            "SELECT charges FROM transactions ORDER BY date").fetchone()[0] == L(42.50)

    def test_matches_existing_instrument_by_isin(self, conn):
        conn.execute(
            "INSERT INTO instruments (kind, name, symbol, isin, asset_class, tax_regime)"
            " VALUES ('EQUITY','Reliance Industries','RIL','INE002A01018','EQUITY','EQUITY')"
        )
        r = tradebook.import_trades(conn, tradebook.parse_csv(ZERODHA))
        assert r.instruments_created == 1       # only INFY is new
        assert conn.execute(
            "SELECT COUNT(*) FROM instruments WHERE isin='INE002A01018'").fetchone()[0] == 1

    def test_dry_run_writes_nothing(self, conn):
        r = tradebook.import_trades(conn, tradebook.parse_csv(ZERODHA), dry_run=True)
        assert r.transactions_added == 3
        assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 0

    def test_imported_trades_build_correct_lots(self, conn):
        """End to end: a tradebook import must produce usable FIFO lots."""
        from fpa.lots import open_lots, rebuild

        tradebook.import_trades(conn, tradebook.parse_csv(ZERODHA))
        rebuild(conn)
        lots = open_lots(conn)
        reliance = [l for l in lots if l.remaining_qty == 15]
        assert reliance, "25 bought minus 10 sold should leave 15"
        assert conn.execute("SELECT COUNT(*) FROM disposals").fetchone()[0] == 1
