from __future__ import annotations

from fpa.lots import open_lots, realised_gains, rebuild
from fpa.money import to_paise
from fpa.tax.engine import Term

L = to_paise


class TestFifo:
    def test_sale_consumes_oldest_lot_first(self, conn, add):
        i = add.instrument()
        add.buy(i, days_ago=800, qty=10, price_rs=100)
        add.buy(i, days_ago=100, qty=10, price_rs=200)
        add.sell(i, days_ago=10, qty=10, price_rs=300)
        rebuild(conn)

        disposals = conn.execute("SELECT * FROM disposals").fetchall()
        assert len(disposals) == 1
        # Oldest lot: cost 100, sale 300 -> 200/share gain on 10 shares.
        assert disposals[0]["gain"] == L(2_000)
        assert disposals[0]["term"] == Term.LONG.value

    def test_sale_spanning_two_lots_splits_by_term(self, conn, add):
        i = add.instrument()
        add.buy(i, days_ago=800, qty=10, price_rs=100)   # long-term
        add.buy(i, days_ago=100, qty=10, price_rs=200)   # short-term
        add.sell(i, days_ago=1, qty=15, price_rs=300)
        rebuild(conn)

        rows = conn.execute("SELECT * FROM disposals ORDER BY id").fetchall()
        assert len(rows) == 2
        assert rows[0]["quantity"] == 10 and rows[0]["term"] == Term.LONG.value
        assert rows[1]["quantity"] == 5 and rows[1]["term"] == Term.SHORT.value
        assert rows[1]["gain"] == L(500)  # 5 * (300 - 200)

    def test_remaining_quantity_tracked(self, conn, add):
        i = add.instrument()
        add.buy(i, days_ago=500, qty=100, price_rs=50)
        add.sell(i, days_ago=10, qty=30, price_rs=80)
        rebuild(conn)

        lots = open_lots(conn)
        assert len(lots) == 1
        assert lots[0].remaining_qty == 70


class TestCosts:
    def test_buy_charges_raise_cost_basis(self, conn, add):
        i = add.instrument()
        add.buy(i, days_ago=500, qty=10, price_rs=100, charges_rs=50)
        rebuild(conn)
        # (1000 + 50) / 10 = 105 per share
        assert open_lots(conn)[0].cost_per_unit == L(105)

    def test_sell_charges_reduce_consideration(self, conn, add):
        i = add.instrument()
        add.buy(i, days_ago=500, qty=10, price_rs=100)
        add.sell(i, days_ago=10, qty=10, price_rs=200, charges_rs=100)
        rebuild(conn)
        # Proceeds 2000 - 100 brokerage = 1900; cost 1000; gain 900.
        assert conn.execute("SELECT gain FROM disposals").fetchone()[0] == L(900)


class TestCorporateActions:
    def test_bonus_creates_a_zero_cost_lot(self, conn, add):
        i = add.instrument()
        add.buy(i, days_ago=500, qty=10, price_rs=100)
        conn.execute(
            "INSERT INTO transactions (instrument_id, date, kind, quantity, amount)"
            " VALUES (?,?,'BONUS',10,0)",
            (i, "2026-01-01"),
        )
        rebuild(conn)
        lots = open_lots(conn)
        assert len(lots) == 2
        assert lots[1].cost_per_unit == 0

    def test_split_scales_quantity_and_cost(self, conn, add):
        i = add.instrument()
        add.buy(i, days_ago=500, qty=10, price_rs=1000)
        conn.execute(
            "INSERT INTO transactions (instrument_id, date, kind, quantity, amount)"
            " VALUES (?,?,'SPLIT',5,0)",
            (i, "2026-01-01"),
        )
        rebuild(conn)
        lot = open_lots(conn)[0]
        assert lot.remaining_qty == 50
        assert lot.cost_per_unit == L(200)   # total cost unchanged


class TestRebuild:
    def test_rebuild_is_idempotent(self, conn, add):
        i = add.instrument()
        add.buy(i, days_ago=500, qty=10, price_rs=100)
        add.sell(i, days_ago=10, qty=5, price_rs=150)
        first = rebuild(conn)
        second = rebuild(conn)
        assert first == second
        assert len(open_lots(conn)) == 1

    def test_overselling_is_reported_not_silently_absorbed(self, conn, add):
        i = add.instrument()
        add.buy(i, days_ago=500, qty=5, price_rs=100)
        add.sell(i, days_ago=10, qty=50, price_rs=150)
        assert rebuild(conn)["unmatched_sells"] == 1

    def test_realised_gains_filter_by_fy(self, conn, add):
        i = add.instrument()
        add.buy(i, days_ago=900, qty=10, price_rs=100)
        add.sell(i, days_ago=10, qty=10, price_rs=200)   # 2026-08-03 -> FY 2026-27
        rebuild(conn)
        assert len(realised_gains(conn, "2026-27")) == 1
        assert realised_gains(conn, "2024-25") == []
