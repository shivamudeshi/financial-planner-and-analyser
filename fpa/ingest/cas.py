"""CAMS / KFintech Consolidated Account Statement parser.

A CAS is the authoritative record of your mutual fund holdings: every folio
across every AMC, with transaction-level detail, units and cost. Request the
**detailed** statement (not the summary) from camsonline.com or kfintech.com and
you get a password-protected PDF covering the whole MF side of your portfolio in
one file. That is why this is the import path rather than manual entry.

## The reconciliation guarantee

Statement layouts vary between CAMS and KFintech, change over time, and differ
across AMCs. A parser that silently mis-reads one line produces a wrong cost
basis, which produces a wrong capital gain, which produces a wrong tax number —
the kind of failure you would not notice until it mattered.

So every scheme is **reconciled before import**: the CAS states its own
``Closing Unit Balance``, and the parser recomputes that balance from the
transactions it extracted. If the two disagree beyond rounding, the scheme is
rejected with the discrepancy shown, not imported. A parse that cannot prove
itself correct does not get to write to your ledger.

Run with ``--dry-run`` first to see exactly what was understood.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from ..money import to_paise

UNIT_TOLERANCE = 0.01  # units; CAS rounds to 3dp and AMCs differ in the last place


# Amounts: 1,234.56  or  (1,234.56) for negatives. Units 3dp, NAV 4dp.
_NUM = r"\(?-?[\d,]+\.\d{2,4}\)?"
TXN_RE = re.compile(
    rf"^(?P<date>\d{{2}}-[A-Za-z]{{3}}-\d{{4}})\s+"
    rf"(?P<desc>.+?)\s+"
    rf"(?P<amount>{_NUM})\s+"
    rf"(?P<units>{_NUM})\s+"
    rf"(?P<nav>{_NUM})\s+"
    rf"(?P<balance>{_NUM})\s*$"
)
FOLIO_RE = re.compile(r"Folio\s*No[:.]?\s*([\w/\- ]+?)(?:\s{2,}|$)", re.I)
ISIN_RE = re.compile(r"\b(INF[A-Z0-9]{9})\b")
AMFI_RE = re.compile(r"\bAMFI\s*Code\s*[:\-]?\s*(\d{4,6})\b", re.I)
OPENING_RE = re.compile(r"Opening\s+Unit\s+Balance[:.]?\s*([\d,]+\.\d+)", re.I)
CLOSING_RE = re.compile(r"Closing\s+Unit\s+Balance[:.]?\s*([\d,]+\.\d+)", re.I)
PERIOD_RE = re.compile(
    r"(\d{2}-[A-Za-z]{3}-\d{4})\s*(?:To|to|-)\s*(\d{2}-[A-Za-z]{3}-\d{4})"
)
# Charge rows carry no units and must never become transactions.
SKIP_RE = re.compile(r"\*{2,}|stamp\s*duty|stt\s*paid|tds|transaction\s*charge", re.I)

# Description -> ledger transaction kind. Order matters: the first match wins,
# so more specific phrases are listed first.
KIND_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"switch\s*[-/ ]?\s*out|switch\s+from", re.I), "SWITCH_OUT"),
    (re.compile(r"switch\s*[-/ ]?\s*in|switch\s+to", re.I), "SWITCH_IN"),
    (re.compile(r"redemption|redeem|repurchase|\bsale\b", re.I), "SELL"),
    (re.compile(r"systematic\s+investment|sip\b|purchase\s*-\s*sip", re.I), "SIP"),
    (re.compile(r"reinvest", re.I), "BUY"),
    (re.compile(r"bonus", re.I), "BONUS"),
    (re.compile(r"dividend|idcw|payout", re.I), "DIVIDEND"),
    (re.compile(r"purchase|investment|subscription", re.I), "BUY"),
]


@dataclass
class CasTransaction:
    date: str
    description: str
    amount: int          # paise, absolute
    units: float         # signed: negative for redemptions
    nav: int             # paise
    balance: float
    kind: str

    @property
    def signed_units(self) -> float:
        return self.units


@dataclass
class CasScheme:
    name: str
    folio: str
    isin: str | None = None
    amfi_code: str | None = None
    amc: str | None = None
    opening_balance: float = 0.0
    closing_balance: float | None = None
    transactions: list[CasTransaction] = field(default_factory=list)

    @property
    def computed_balance(self) -> float:
        return self.opening_balance + sum(t.units for t in self.transactions)

    @property
    def reconciles(self) -> bool:
        """Does the CAS's own closing balance match what we parsed?"""
        if self.closing_balance is None:
            return False
        return abs(self.computed_balance - self.closing_balance) <= UNIT_TOLERANCE

    @property
    def is_complete(self) -> bool:
        """Does this statement cover the scheme's entire history?

        A non-zero opening balance means units were acquired *before* the
        statement period. Those units have no purchase transaction in this file,
        so their cost basis and acquisition date are unknown — and cost basis
        and acquisition date are exactly what capital-gains tax turns on.
        Importing anyway would produce a holding that silently understates cost
        and misdates the 12-month line.
        """
        return abs(self.opening_balance) <= UNIT_TOLERANCE

    @property
    def discrepancy(self) -> float:
        if self.closing_balance is None:
            return 0.0
        return self.computed_balance - self.closing_balance

    def key(self) -> str:
        return self.isin or self.amfi_code or self.name


@dataclass
class CasStatement:
    period_from: str | None = None
    period_to: str | None = None
    schemes: list[CasScheme] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def reconciled(self) -> list[CasScheme]:
        return [s for s in self.schemes if s.reconciles]

    @property
    def failed(self) -> list[CasScheme]:
        return [s for s in self.schemes if not s.reconciles]

    @property
    def importable(self) -> list[CasScheme]:
        return [s for s in self.schemes if s.reconciles and s.is_complete]

    @property
    def incomplete(self) -> list[CasScheme]:
        """Reconciled, but the statement starts mid-history."""
        return [s for s in self.schemes if s.reconciles and not s.is_complete]

    @property
    def transaction_count(self) -> int:
        return sum(len(s.transactions) for s in self.schemes)


# -- extraction -----------------------------------------------------------


def extract_text(path: Path | str, password: str | None = None) -> str:
    """Pull text out of a (usually encrypted) CAS PDF.

    The password is normally your PAN in caps, or whatever you set when
    requesting the statement.
    """
    from pypdf import PdfReader
    from pypdf.errors import FileNotDecryptedError

    reader = PdfReader(str(path))
    if reader.is_encrypted:
        if password is None:
            raise ValueError(
                "This CAS is password-protected. Pass the password you set when "
                "requesting it (often your PAN in capitals)."
            )
        try:
            if reader.decrypt(password) == 0:
                raise ValueError("Incorrect CAS password.")
        except FileNotDecryptedError as exc:
            raise ValueError("Incorrect CAS password.") from exc

    return "\n".join(page.extract_text() or "" for page in reader.pages)


# -- parsing --------------------------------------------------------------


def _num(raw: str) -> float:
    """CAS numbers: commas as separators, parentheses for negatives."""
    raw = raw.strip()
    negative = raw.startswith("(") and raw.endswith(")")
    value = float(raw.strip("()").replace(",", ""))
    return -value if negative else value


def _date(raw: str) -> str:
    return datetime.strptime(raw, "%d-%b-%Y").date().isoformat()


def classify(description: str) -> str | None:
    for pattern, kind in KIND_PATTERNS:
        if pattern.search(description):
            return kind
    return None


def parse_text(text: str) -> CasStatement:
    """Parse extracted CAS text into schemes and transactions.

    Written against the layout of the CAMS/KFintech detailed statement. Anything
    it cannot confidently interpret is recorded as a warning rather than guessed
    at, and the reconciliation check in :func:`import_statement` is what actually
    protects the ledger.
    """
    statement = CasStatement()
    scheme: CasScheme | None = None
    folio = ""
    unparsed = 0

    if (m := PERIOD_RE.search(text)):
        statement.period_from, statement.period_to = _date(m.group(1)), _date(m.group(2))

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        if (m := FOLIO_RE.search(line)):
            folio = m.group(1).strip()

        # A line carrying an ISIN starts a new scheme block.
        if (m := ISIN_RE.search(line)) and not TXN_RE.match(line):
            if scheme is not None:
                statement.schemes.append(scheme)
            scheme = CasScheme(
                name=_scheme_name(line, m.group(1)),
                folio=folio,
                isin=m.group(1),
                amfi_code=(a.group(1) if (a := AMFI_RE.search(line)) else None),
            )
            continue

        if scheme is None:
            continue

        if (m := OPENING_RE.search(line)):
            scheme.opening_balance = _num(m.group(1))
            continue
        if (m := CLOSING_RE.search(line)):
            scheme.closing_balance = _num(m.group(1))
            continue

        if (m := TXN_RE.match(line)):
            desc = m.group("desc").strip()
            if SKIP_RE.search(desc):
                continue  # stamp duty, STT and friends are charges, not trades
            kind = classify(desc)
            if kind is None:
                unparsed += 1
                statement.warnings.append(f"Unrecognised transaction type: {desc!r}")
                continue
            units = _num(m.group("units"))
            # Redemptions are sometimes printed unsigned; the description is
            # authoritative about direction.
            if kind in ("SELL", "SWITCH_OUT") and units > 0:
                units = -units
            scheme.transactions.append(
                CasTransaction(
                    date=_date(m.group("date")),
                    description=desc,
                    amount=to_paise(abs(_num(m.group("amount")))),
                    units=units,
                    nav=to_paise(abs(_num(m.group("nav")))),
                    balance=_num(m.group("balance")),
                    kind=kind,
                )
            )

    if scheme is not None:
        statement.schemes.append(scheme)

    if not statement.schemes:
        statement.warnings.append(
            "No schemes found. This is usually a summary-only CAS — request the "
            "*detailed* statement, which includes transaction history."
        )
    if unparsed:
        statement.warnings.append(f"{unparsed} transaction line(s) could not be classified.")
    return statement


def _scheme_name(line: str, isin: str) -> str:
    """Strip the scaffolding off a scheme header line."""
    name = line.replace(isin, "")
    name = re.sub(r"\bISIN\s*[:\-]?\s*", "", name, flags=re.I)
    name = re.sub(r"\(Advisor\s*:.*?\)", "", name, flags=re.I)
    name = re.sub(r"Registrar\s*[:\-]?\s*\w+", "", name, flags=re.I)
    name = re.sub(r"AMFI\s*Code\s*[:\-]?\s*\d+", "", name, flags=re.I)
    name = re.sub(r"^[\w\d]+\s*-\s*", "", name.strip())  # leading scheme code
    return re.sub(r"\s{2,}", " ", name).strip(" -\t")


def parse(path: Path | str, password: str | None = None) -> CasStatement:
    return parse_text(extract_text(path, password))


# -- import ---------------------------------------------------------------


@dataclass
class ImportResult:
    schemes_created: int = 0
    schemes_matched: int = 0
    transactions_added: int = 0
    duplicates_skipped: int = 0
    rejected: list[tuple[str, float]] = field(default_factory=list)
    incomplete: list[tuple[str, float]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def external_id(scheme: CasScheme, txn: CasTransaction) -> str:
    """Stable dedupe key so re-importing overlapping statements is safe."""
    raw = f"{scheme.key()}|{scheme.folio}|{txn.date}|{txn.kind}|{txn.amount}|{txn.units:.4f}"
    return "cas-" + hashlib.sha1(raw.encode()).hexdigest()[:16]


def import_statement(
    conn: sqlite3.Connection,
    statement: CasStatement,
    *,
    dry_run: bool = False,
    allow_partial: bool = False,
) -> ImportResult:
    """Write reconciled, complete schemes into the ledger.

    Two classes of scheme are held back:

    * **Unreconciled** — parsed transactions do not add up to the CAS's own
      closing balance, so the parse is wrong somewhere.
    * **Incomplete** — a non-zero opening balance means the statement starts
      mid-history, so some units have no purchase record and therefore no cost
      basis. Override with ``allow_partial`` only if you will supply the missing
      opening lots yourself.
    """
    result = ImportResult()

    for scheme in statement.failed:
        result.rejected.append((scheme.name, scheme.discrepancy))
    for scheme in statement.incomplete:
        result.incomplete.append((scheme.name, scheme.opening_balance))

    if statement.failed:
        result.notes.append(
            f"{len(statement.failed)} scheme(s) rejected: the transactions parsed do not add up "
            f"to the closing balance the statement itself reports. Importing them would put a "
            f"wrong cost basis into your tax numbers. Re-run with --dry-run to inspect."
        )
    if statement.incomplete and not allow_partial:
        names = ", ".join(n for n, _ in result.incomplete[:3])
        result.notes.append(
            f"{len(statement.incomplete)} scheme(s) held back because the statement begins "
            f"mid-history ({names}{'...' if len(result.incomplete) > 3 else ''}). Those opening "
            f"units have no purchase record here, so their cost basis and holding period are "
            f"unknown. Request a CAS covering the period *from inception* — CAMS and KFintech "
            f"both let you choose the date range. Use --allow-partial to import anyway and "
            f"supply the opening lots yourself."
        )

    targets = statement.reconciled if allow_partial else statement.importable
    for scheme in targets:
        instrument_id, created = _resolve_instrument(conn, scheme, dry_run=dry_run)
        result.schemes_created += created
        result.schemes_matched += 1 - created

        for txn in scheme.transactions:
            ext = external_id(scheme, txn)
            if conn.execute(
                "SELECT 1 FROM transactions WHERE external_id=?", (ext,)
            ).fetchone():
                result.duplicates_skipped += 1
                continue
            if dry_run or instrument_id is None:
                result.transactions_added += 1
                continue
            conn.execute(
                "INSERT INTO transactions (instrument_id, date, kind, quantity, price, amount,"
                " charges, account, external_id, note) VALUES (?,?,?,?,?,?,0,?,?,?)",
                (instrument_id, txn.date, txn.kind, abs(txn.units), txn.nav, txn.amount,
                 scheme.folio, ext, txn.description),
            )
            result.transactions_added += 1

    if not dry_run:
        conn.commit()
    else:
        result.notes.append("Dry run — nothing was written.")
    return result


def _resolve_instrument(
    conn: sqlite3.Connection, scheme: CasScheme, *, dry_run: bool
) -> tuple[int | None, int]:
    """Find the instrument for a CAS scheme, creating it if new.

    Matched on ISIN first (stable), then AMFI code, then exact name.
    """
    for column, value in (("isin", scheme.isin), ("amfi_code", scheme.amfi_code),
                          ("name", scheme.name)):
        if not value:
            continue
        row = conn.execute(
            f"SELECT id FROM instruments WHERE {column}=? AND kind='MF'", (value,)
        ).fetchone()
        if row:
            return row["id"], 0

    if dry_run:
        return None, 1
    iid = conn.execute(
        "INSERT INTO instruments (kind, name, isin, amfi_code, asset_class, tax_regime)"
        " VALUES ('MF',?,?,?,'EQUITY','EQUITY')",
        (scheme.name, scheme.isin, scheme.amfi_code),
    ).lastrowid
    return iid, 1
