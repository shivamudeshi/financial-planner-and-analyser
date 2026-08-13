"""CAS parser tests.

The fixtures below mirror the layout of a CAMS/KFintech *detailed* statement as
extracted to text. The most important tests are the ones where the parser
refuses to import: a wrong cost basis flows straight into a wrong tax number, so
"fails loudly" beats "imports something".
"""

from __future__ import annotations

import pytest

from fpa.ingest import cas
from fpa.money import to_paise

L = to_paise

GOOD_CAS = """
CAMS
Consolidated Account Statement
01-Apr-2025 To 31-Mar-2026

PAN: ABCDE1234F

HDFC Mutual Fund
Folio No: 12345678 / 90    PAN: ABCDE1234F   KYC: OK
HDFC0000123-HDFC Flexi Cap Fund - Growth Plan - ISIN: INF179K01BC2 (Advisor: DIRECT) Registrar : CAMS
Opening Unit Balance: 100.000
04-Apr-2025 Purchase 5,000.00 50.000 100.0000 150.000
04-Apr-2025 *** Stamp Duty *** 0.25 0.000 0.0000 150.000
04-May-2025 Purchase - SIP 5,000.00 45.455 110.0000 195.455
10-Jun-2025 Redemption (2,000.00) (16.667) 120.0000 178.788
Closing Unit Balance: 178.788 NAV on 31-Mar-2026: INR 125.0000

Parag Parikh Mutual Fund
Folio No: 99887766 / 11    PAN: ABCDE1234F
PPFAS001-Parag Parikh Flexi Cap Fund - Direct Growth - ISIN: INF879O01019 Registrar : KFINTECH
Opening Unit Balance: 0.000
15-Apr-2025 Systematic Investment 10,000.00 172.414 58.0000 172.414
15-May-2025 Systematic Investment 10,000.00 166.667 60.0000 339.081
Closing Unit Balance: 339.081 NAV on 31-Mar-2026: INR 72.5000
"""

# Same statement with one instalment dropped, as a mis-parse would produce.
BROKEN_CAS = """
Consolidated Account Statement
01-Apr-2025 To 31-Mar-2026
HDFC0000123-HDFC Flexi Cap Fund - Growth Plan - ISIN: INF179K01BC2 Registrar : CAMS
Folio No: 12345678 / 90
Opening Unit Balance: 100.000
04-Apr-2025 Purchase 5,000.00 50.000 100.0000 150.000
Closing Unit Balance: 178.788 NAV on 31-Mar-2026: INR 125.0000
"""

SUMMARY_ONLY_CAS = """
Consolidated Account Statement
01-Apr-2025 To 31-Mar-2026
PORTFOLIO SUMMARY
HDFC Mutual Fund   1,25,000.00   1,42,000.00
Parag Parikh Mutual Fund   80,000.00   96,500.00
"""


@pytest.fixture
def good() -> cas.CasStatement:
    return cas.parse_text(GOOD_CAS)


class TestParsing:
    def test_finds_every_scheme(self, good):
        assert len(good.schemes) == 2
        assert good.schemes[0].isin == "INF179K01BC2"
        assert good.schemes[1].isin == "INF879O01019"

    def test_reads_the_statement_period(self, good):
        assert good.period_from == "2025-04-01"
        assert good.period_to == "2026-03-31"

    def test_captures_folio_per_scheme(self, good):
        assert good.schemes[0].folio == "12345678 / 90"
        assert good.schemes[1].folio == "99887766 / 11"

    def test_cleans_the_scheme_name(self, good):
        assert good.schemes[0].name == "HDFC Flexi Cap Fund - Growth Plan"
        assert "ISIN" not in good.schemes[0].name
        assert "Advisor" not in good.schemes[0].name

    def test_parses_amounts_units_and_nav(self, good):
        t = good.schemes[0].transactions[0]
        assert t.date == "2025-04-04"          # 04-Apr-2025
        assert t.amount == L(5_000)
        assert t.units == 50.0
        assert t.nav == L(100)

    def test_skips_charge_rows(self, good):
        """Stamp duty and STT are charges, not trades — they must never become
        transactions or the unit balance stops reconciling."""
        descs = [t.description for t in good.schemes[0].transactions]
        assert not any("Stamp Duty" in d for d in descs)
        assert len(good.schemes[0].transactions) == 3


class TestClassification:
    @pytest.mark.parametrize("desc,kind", [
        ("Purchase", "BUY"),
        ("Purchase - SIP", "SIP"),
        ("Systematic Investment", "SIP"),
        ("Redemption", "SELL"),
        ("Sale", "SELL"),
        ("Switch Out - to HDFC Balanced", "SWITCH_OUT"),
        ("Switch In - from HDFC Top 100", "SWITCH_IN"),
        ("Dividend Payout", "DIVIDEND"),
        ("IDCW Reinvestment", "BUY"),
        ("Bonus Units", "BONUS"),
    ])
    def test_descriptions_map_to_ledger_kinds(self, desc, kind):
        assert cas.classify(desc) == kind

    def test_switch_out_beats_the_generic_sale_pattern(self):
        # Ordering matters: 'Switch Out' also contains no 'sale', but
        # 'Switch Out - Sale' would match both.
        assert cas.classify("Switch Out - Sale of units") == "SWITCH_OUT"

    def test_unknown_description_is_reported_not_guessed(self):
        s = cas.parse_text(GOOD_CAS.replace("Purchase 5,000.00", "Frobnicate 5,000.00"))
        assert any("Unrecognised" in w for w in s.warnings)


class TestSigns:
    def test_redemption_units_are_negative(self, good):
        assert good.schemes[0].transactions[2].units == -16.667

    def test_unsigned_redemption_is_corrected_from_the_description(self):
        """Some statements print redemption units without parentheses. The
        description is authoritative about direction."""
        s = cas.parse_text(
            GOOD_CAS.replace("Redemption (2,000.00) (16.667)", "Redemption 2,000.00 16.667")
        )
        assert s.schemes[0].transactions[2].units == -16.667
        assert s.schemes[0].reconciles


class TestReconciliation:
    def test_good_statement_reconciles(self, good):
        assert all(s.reconciles for s in good.schemes)
        assert good.schemes[0].computed_balance == pytest.approx(178.788)
        assert good.schemes[1].computed_balance == pytest.approx(339.081)

    def test_missing_transaction_fails_reconciliation(self):
        s = cas.parse_text(BROKEN_CAS)
        assert not s.schemes[0].reconciles
        assert s.schemes[0].discrepancy == pytest.approx(-28.788)

    def test_summary_only_cas_is_diagnosed(self):
        s = cas.parse_text(SUMMARY_ONLY_CAS)
        assert s.schemes == []
        assert any("detailed" in w for w in s.warnings)


class TestImport:
    def test_imports_only_schemes_with_complete_history(self, conn, good):
        """HDFC opens with 100 units carried in from before the statement, so
        it is held back; Parag Parikh opens at zero and imports."""
        r = cas.import_statement(conn, good)
        assert r.schemes_created == 1
        assert r.transactions_added == 2
        assert len(r.incomplete) == 1
        assert r.incomplete[0] == ("HDFC Flexi Cap Fund - Growth Plan", 100.0)
        assert any("from inception" in n for n in r.notes)

    def test_allow_partial_overrides_the_opening_balance_guard(self, conn, good):
        r = cas.import_statement(conn, good, allow_partial=True)
        assert r.schemes_created == 2
        assert r.transactions_added == 5

    def test_unreconciled_scheme_is_rejected_not_imported(self, conn):
        r = cas.import_statement(conn, cas.parse_text(BROKEN_CAS))
        assert r.transactions_added == 0
        assert len(r.rejected) == 1
        assert r.rejected[0][0] == "HDFC Flexi Cap Fund - Growth Plan"
        assert any("wrong cost basis" in n for n in r.notes)
        assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 0

    def test_reimport_is_idempotent(self, conn, good):
        cas.import_statement(conn, good, allow_partial=True)
        second = cas.import_statement(conn, cas.parse_text(GOOD_CAS), allow_partial=True)
        assert second.transactions_added == 0
        assert second.duplicates_skipped == 5
        assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 5

    def test_matches_existing_instrument_by_isin(self, conn, good):
        conn.execute(
            "INSERT INTO instruments (kind, name, isin, asset_class, tax_regime)"
            " VALUES ('MF','My Existing Name','INF179K01BC2','EQUITY','EQUITY')"
        )
        r = cas.import_statement(conn, good, allow_partial=True)
        assert r.schemes_created == 1        # only the second scheme is new
        assert r.schemes_matched == 1
        assert conn.execute(
            "SELECT name FROM instruments WHERE isin='INF179K01BC2'"
        ).fetchone()[0] == "My Existing Name"

    def test_dry_run_writes_nothing(self, conn, good):
        r = cas.import_statement(conn, good, dry_run=True, allow_partial=True)
        assert r.transactions_added == 5
        assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 0
        assert any("Dry run" in n for n in r.notes)

    def test_folio_is_recorded_on_each_transaction(self, conn, good):
        cas.import_statement(conn, good, allow_partial=True)
        accounts = {r[0] for r in conn.execute("SELECT DISTINCT account FROM transactions")}
        assert accounts == {"12345678 / 90", "99887766 / 11"}


class TestPdfLayer:
    """Round-trip through an actual encrypted PDF.

    The text fixtures above test the parsing rules; this tests the part that
    touches a real file — password handling and text extraction — which is where
    a live CAS is most likely to trip.
    """

    @pytest.fixture
    def encrypted_pdf(self, tmp_path):
        reportlab = pytest.importorskip("reportlab", reason="reportlab not installed")
        from pypdf import PdfReader, PdfWriter
        from reportlab.lib.pagesizes import A4
        from reportlab.pdfgen import canvas

        path = tmp_path / "cas.pdf"
        c = canvas.Canvas(str(path), pagesize=A4)
        c.setFont("Helvetica", 8)
        y = 800
        for line in GOOD_CAS.splitlines():
            c.drawString(28, y, line)
            y -= 13
            if y < 40:
                c.showPage()
                c.setFont("Helvetica", 8)
                y = 800
        c.save()

        reader, writer = PdfReader(str(path)), PdfWriter()
        for page in reader.pages:
            writer.add_page(page)
        writer.encrypt("ABCDE1234F")
        with open(path, "wb") as fh:
            writer.write(fh)
        return path

    def test_missing_password_says_what_to_do(self, encrypted_pdf):
        with pytest.raises(ValueError, match="password-protected"):
            cas.extract_text(encrypted_pdf)

    def test_wrong_password_is_distinguished_from_missing(self, encrypted_pdf):
        with pytest.raises(ValueError, match="Incorrect CAS password"):
            cas.extract_text(encrypted_pdf, "NOPE")

    def test_parses_and_reconciles_from_a_real_pdf(self, encrypted_pdf):
        statement = cas.parse(encrypted_pdf, "ABCDE1234F")
        assert len(statement.schemes) == 2
        assert all(s.reconciles for s in statement.schemes)
        assert statement.transaction_count == 5

    def test_end_to_end_import_builds_lots(self, conn, encrypted_pdf):
        from fpa.lots import open_lots, rebuild

        cas.import_statement(conn, cas.parse(encrypted_pdf, "ABCDE1234F"))
        rebuild(conn)
        lots = open_lots(conn)
        # Only Parag Parikh imports; HDFC is held back for its opening balance.
        assert conn.execute("SELECT COUNT(*) FROM instruments").fetchone()[0] == 1
        assert sum(l.remaining_qty for l in lots) == pytest.approx(339.081, abs=0.01)


class TestNumberParsing:
    @pytest.mark.parametrize("raw,expected", [
        ("1,234.56", 1234.56),
        ("(1,234.56)", -1234.56),
        ("0.000", 0.0),
        ("1,23,456.78", 123456.78),      # Indian grouping
    ])
    def test_handles_indian_number_formats(self, raw, expected):
        assert cas._num(raw) == pytest.approx(expected)
