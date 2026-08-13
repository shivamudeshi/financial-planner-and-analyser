"""Command line entry points.

    python -m fpa.cli sample      # generate a sample portfolio
    python -m fpa.cli rebuild     # rebuild lots/disposals from transactions
    python -m fpa.cli refresh     # pull latest AMFI NAVs and equity prices
    python -m fpa.cli plan        # print this FY's zero-tax sell plan
"""

from __future__ import annotations

import argparse
import sys
from datetime import date as Date

from .db import DEFAULT_DB, connect, financial_year
from .lots import rebuild
from .money import fmt, fmt_compact
from .planner.sell_planner import Mode, SellPlanner
from .tax.engine import TaxEngine, Term


def cmd_sample(args) -> int:
    from .sample_data import generate

    conn = connect(args.db)
    stats = generate(conn, as_of=args.as_of)
    lot_stats = rebuild(conn)
    print(f"Generated {stats['instruments']} instruments, {stats['transactions']} transactions, "
          f"{stats['prices']:,} price rows.")
    print(f"Built {lot_stats['lots']} lots and {lot_stats['disposals']} disposals.")
    print(f"\nDatabase: {args.db}\nNow run:  streamlit run app.py")
    return 0


def cmd_rebuild(args) -> int:
    conn = connect(args.db)
    stats = rebuild(conn)
    print(f"Rebuilt {stats['lots']} lots, {stats['disposals']} disposals.")
    if stats["unmatched_sells"]:
        print(f"⚠  {stats['unmatched_sells']} sale(s) exceeded known holdings — "
              f"you are probably missing a buy import.", file=sys.stderr)
    return 1 if stats["unmatched_sells"] else 0


def cmd_refresh(args) -> int:
    from .ingest import amfi, equity_prices

    conn = connect(args.db)
    try:
        n = amfi.update_navs(conn)
        print(f"AMFI: updated {n} NAV(s).")
    except Exception as exc:
        print(f"AMFI refresh failed: {exc}", file=sys.stderr)

    try:
        stats = equity_prices.update_prices(conn, period=args.period)
        print(f"Equities: {stats['rows']:,} rows across {stats['instruments']} instrument(s), "
              f"{stats['failed']} failed.")
    except Exception as exc:
        print(f"Equity refresh failed: {exc}", file=sys.stderr)

    rebuild(conn)
    return 0


def cmd_plan(args) -> int:
    conn = connect(args.db)
    as_of = args.as_of or Date.today().isoformat()
    fy = args.fy or financial_year(as_of)
    planner = SellPlanner(conn, TaxEngine(fy), fy=fy, as_of=as_of)
    plan = planner.plan(Mode[args.mode], min_priority=args.min_priority)

    print(f"\n  Zero-tax sell plan — FY {fy}, as of {as_of}, mode {args.mode}")
    print(f"  {'─' * 76}")
    if not plan.actions:
        print("  Nothing sellable at zero tax under these settings.\n")
    for ip in plan.by_instrument():
        flag = "" if ip.is_full_exit else "  (partial)"
        print(f"  {(ip.symbol or ip.name)[:34]:<34} {fmt_compact(ip.proceeds):>10}"
              f"  gain {fmt_compact(ip.gain):>9}{flag}")
        print(f"      {ip.summary()}")
        if args.lots:
            for a in sorted(ip.actions, key=lambda a: a.buy_date):
                term = "LT" if a.term is Term.LONG else "ST"
                print(f"        · {a.buy_date}  {a.quantity:>10,.3f} @ "
                      f"{fmt(a.price)}  gain {fmt(a.gain):>10} [{term}]")

    print(f"\n  Proceeds {fmt(plan.proceeds)}   gain realised {fmt(plan.gain_realised)}   "
          f"tax {fmt(plan.tax)}")
    print(f"  Exemption left: {fmt(plan.exemption_left)}   "
          f"avoided vs selling outright: {fmt(plan.tax_saved)}")

    deferrals = plan.deferrals_by_instrument()
    if deferrals:
        print("\n  Wait, don't sell:")
        for d in deferrals[:10]:
            print(f"    {d['name'][:34]:<34} fully long-term in {d['days_to_long_term']:>3}d "
                  f"({d['long_term_date']}) — saves {fmt(d['tax_if_sold_now'])}")
    for w in plan.warnings:
        print(f"\n  ⚠  {w}")
    print()
    for n in plan.notes:
        print(f"  · {n}")
    print()
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="fpa", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--db", default=str(DEFAULT_DB))
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("sample", help="generate a sample portfolio")
    s.add_argument("--as-of", default=None)
    s.set_defaults(func=cmd_sample)

    s = sub.add_parser("rebuild", help="rebuild derived lots and disposals")
    s.set_defaults(func=cmd_rebuild)

    s = sub.add_parser("refresh", help="fetch latest NAVs and prices")
    s.add_argument("--period", default="5y")
    s.set_defaults(func=cmd_refresh)

    s = sub.add_parser("plan", help="print the zero-tax sell plan")
    s.add_argument("--mode", choices=[m.value for m in Mode], default=Mode.EXIT.value)
    s.add_argument("--min-priority", type=int, default=0)
    s.add_argument("--lots", action="store_true", help="show lot-level detail under each order")
    s.add_argument("--fy", default=None)
    s.add_argument("--as-of", default=None)
    s.set_defaults(func=cmd_plan)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
