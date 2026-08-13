"""Mutual fund NAV from AMFI. Free, no API key, no registration.

AMFI publishes every scheme's NAV as a semicolon-delimited text file, refreshed
around 11pm IST on business days. That single file covers the entire Indian MF
universe, which is why phase 1 of this app needs no paid data at all.

``mfapi.in`` is an unofficial mirror that exposes AMFI history per scheme. It is
used only for backfill and treated as best-effort — if it is unavailable, the
daily file still keeps current NAVs correct.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime

import requests

from ..money import to_paise

NAV_ALL_URL = "https://www.amfiindia.com/spages/NAVAll.txt"
HISTORY_URL = "https://api.mfapi.in/mf/{code}"
TIMEOUT = 30


def _iso(d: str) -> str:
    """AMFI prints '13-Aug-2026'."""
    return datetime.strptime(d.strip(), "%d-%b-%Y").date().isoformat()


def fetch_nav_all(url: str = NAV_ALL_URL) -> list[dict]:
    """Parse the full AMFI NAV file into scheme records.

    The file interleaves fund-house headings and blank lines between data rows,
    so anything without the expected six fields is skipped rather than parsed.
    """
    resp = requests.get(url, timeout=TIMEOUT)
    resp.raise_for_status()

    out = []
    for line in resp.text.splitlines():
        parts = line.split(";")
        if len(parts) != 6 or parts[0].strip() == "Scheme Code":
            continue
        code, isin_growth, _isin_reinv, name, nav, date = (p.strip() for p in parts)
        if not code.isdigit() or nav in ("", "N.A."):
            continue
        try:
            out.append(
                {
                    "amfi_code": code,
                    "isin": isin_growth or None,
                    "name": name,
                    "nav": to_paise(nav),
                    "date": _iso(date),
                }
            )
        except ValueError:
            continue  # unparseable NAV or date; skip the row, keep the file
    return out


def update_navs(conn: sqlite3.Connection, records: list[dict] | None = None) -> int:
    """Write today's NAV for every MF already in ``instruments``.

    Only tracked schemes are stored — the file carries ~12k schemes and there is
    no reason to keep NAVs for funds you do not hold.
    """
    records = records if records is not None else fetch_nav_all()
    tracked = {
        r["amfi_code"]: r["id"]
        for r in conn.execute(
            "SELECT id, amfi_code FROM instruments WHERE kind='MF' AND amfi_code IS NOT NULL"
        )
    }
    rows = [
        (tracked[r["amfi_code"]], r["date"], r["nav"], "amfi")
        for r in records
        if r["amfi_code"] in tracked
    ]
    with conn:
        conn.executemany(
            "INSERT OR REPLACE INTO prices (instrument_id, date, close, source)"
            " VALUES (?,?,?,?)",
            rows,
        )
    return len(rows)


def backfill_history(conn: sqlite3.Connection, amfi_code: str) -> int:
    """Pull a scheme's full NAV history from the mfapi.in mirror."""
    row = conn.execute(
        "SELECT id FROM instruments WHERE amfi_code=?", (amfi_code,)
    ).fetchone()
    if row is None:
        raise ValueError(f"Scheme {amfi_code} is not tracked; add it to instruments first")

    resp = requests.get(HISTORY_URL.format(code=amfi_code), timeout=TIMEOUT)
    resp.raise_for_status()
    payload = resp.json()

    rows = []
    for e in payload.get("data", []):
        try:
            d = datetime.strptime(e["date"], "%d-%m-%Y").date().isoformat()
            rows.append((row["id"], d, to_paise(e["nav"]), "mfapi"))
        except (ValueError, KeyError):
            continue
    with conn:
        conn.executemany(
            "INSERT OR REPLACE INTO prices (instrument_id, date, close, source)"
            " VALUES (?,?,?,?)",
            rows,
        )
    return len(rows)


def search(records: list[dict], query: str, limit: int = 25) -> list[dict]:
    """Substring search over scheme names, for picking a scheme code by hand."""
    q = query.lower()
    return [r for r in records if q in r["name"].lower()][:limit]
