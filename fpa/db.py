"""SQLite storage. One file, no server, fully recomputable from ``transactions``.

``transactions`` is the only hand-maintained table. ``lots`` and ``disposals``
are derived and rebuilt wholesale by :mod:`fpa.lots`, so a bad import is fixed
by correcting the transaction and rebuilding, never by patching derived state.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

DEFAULT_DB = Path(__file__).resolve().parent.parent / "data" / "portfolio.db"

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS instruments (
    id            INTEGER PRIMARY KEY,
    kind          TEXT    NOT NULL CHECK (kind IN ('EQUITY','MF')),
    name          TEXT    NOT NULL,
    isin          TEXT    UNIQUE,
    symbol        TEXT,
    exchange      TEXT,
    amfi_code     TEXT    UNIQUE,
    category      TEXT,
    asset_class   TEXT    NOT NULL DEFAULT 'EQUITY',
    tax_regime    TEXT    NOT NULL DEFAULT 'EQUITY' CHECK (tax_regime IN ('EQUITY','OTHER')),
    benchmark_id  INTEGER REFERENCES instruments(id),
    exit_priority INTEGER NOT NULL DEFAULT 50,   -- 0..100, how much you want out
    thesis        TEXT,
    active        INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_instruments_kind ON instruments(kind, active);

-- Unified price/NAV series. MF rows populate close only.
CREATE TABLE IF NOT EXISTS prices (
    instrument_id INTEGER NOT NULL REFERENCES instruments(id) ON DELETE CASCADE,
    date          TEXT    NOT NULL,
    open          INTEGER,
    high          INTEGER,
    low           INTEGER,
    close         INTEGER NOT NULL,
    volume        INTEGER,
    source        TEXT    NOT NULL,
    PRIMARY KEY (instrument_id, date)
);
CREATE INDEX IF NOT EXISTS idx_prices_date ON prices(date);

-- The single source of truth.
CREATE TABLE IF NOT EXISTS transactions (
    id            INTEGER PRIMARY KEY,
    instrument_id INTEGER NOT NULL REFERENCES instruments(id) ON DELETE CASCADE,
    date          TEXT    NOT NULL,
    kind          TEXT    NOT NULL CHECK (kind IN
                    ('BUY','SELL','SIP','DIVIDEND','BONUS','SPLIT','SWITCH_IN','SWITCH_OUT')),
    quantity      REAL    NOT NULL,
    price         INTEGER,
    amount        INTEGER NOT NULL,
    charges       INTEGER NOT NULL DEFAULT 0,
    account       TEXT,
    external_id   TEXT    UNIQUE,
    note          TEXT
);
CREATE INDEX IF NOT EXISTS idx_txn_instrument ON transactions(instrument_id, date);

-- Derived: open tax lots, FIFO.
CREATE TABLE IF NOT EXISTS lots (
    id                 INTEGER PRIMARY KEY,
    instrument_id      INTEGER NOT NULL REFERENCES instruments(id) ON DELETE CASCADE,
    buy_txn_id         INTEGER REFERENCES transactions(id) ON DELETE CASCADE,
    buy_date           TEXT    NOT NULL,
    quantity           REAL    NOT NULL,
    remaining_qty      REAL    NOT NULL,
    cost_per_unit      INTEGER NOT NULL,
    fmv_2018           INTEGER
);
CREATE INDEX IF NOT EXISTS idx_lots_instrument ON lots(instrument_id, buy_date);

-- Derived: realised gains, one row per lot consumed by a sale.
CREATE TABLE IF NOT EXISTS disposals (
    id            INTEGER PRIMARY KEY,
    lot_id        INTEGER NOT NULL REFERENCES lots(id) ON DELETE CASCADE,
    sell_txn_id   INTEGER NOT NULL REFERENCES transactions(id) ON DELETE CASCADE,
    instrument_id INTEGER NOT NULL REFERENCES instruments(id) ON DELETE CASCADE,
    sell_date     TEXT    NOT NULL,
    quantity      REAL    NOT NULL,
    sale_value    INTEGER NOT NULL,
    cost          INTEGER NOT NULL,
    gain          INTEGER NOT NULL,
    days_held     INTEGER NOT NULL,
    term          TEXT    NOT NULL CHECK (term IN ('SHORT_TERM','LONG_TERM')),
    fy            TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_disposals_fy ON disposals(fy);

-- Losses brought forward from years before the ledger starts.
CREATE TABLE IF NOT EXISTS carry_forward_losses (
    fy       TEXT NOT NULL,   -- FY in which the loss arose
    term     TEXT NOT NULL CHECK (term IN ('SHORT_TERM','LONG_TERM')),
    amount   INTEGER NOT NULL,   -- positive magnitude, paise
    consumed INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (fy, term)
);

-- Yearly plan.
CREATE TABLE IF NOT EXISTS plan_targets (
    fy          TEXT NOT NULL,
    asset_class TEXT NOT NULL,
    target_pct  REAL NOT NULL,
    note        TEXT,
    PRIMARY KEY (fy, asset_class)
);

CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


def connect(
    path: Path | str = DEFAULT_DB,
    *,
    create: bool = True,
    same_thread: bool = True,
) -> sqlite3.Connection:
    """Open the database.

    ``same_thread=False`` is needed by Streamlit, which reruns the script on a
    different thread each interaction while the cached connection persists.
    Safe here because only one script run is active per session at a time; do
    not reuse that mode for anything genuinely concurrent.
    """
    path = Path(path)
    if create:
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=same_thread)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    if create:
        conn.executescript(SCHEMA)
    return conn


def financial_year(date: str) -> str:
    """Indian FY for an ISO date. April-March. '2025-06-01' -> '2025-26'."""
    y, m = int(date[:4]), int(date[5:7])
    start = y if m >= 4 else y - 1
    return f"{start}-{str(start + 1)[2:]}"


def fy_bounds(fy: str) -> tuple[str, str]:
    """Inclusive ISO date bounds of an FY key like '2025-26'."""
    start = int(fy[:4])
    return f"{start}-04-01", f"{start + 1}-03-31"
