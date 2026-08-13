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
from .money import fmt, fmt_compact, to_paise
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
    if args.economics:
        _print_economics(conn, plan, engine=planner.engine, as_of=as_of)

    for w in plan.warnings:
        print(f"\n  ⚠  {w}")
    print()
    for n in plan.notes:
        print(f"  · {n}")
    print()
    return 0


def _print_economics(conn, plan, *, engine, as_of: str) -> None:
    """What the tax saving actually costs — the counterweight to the plan."""
    from .planner.opportunity import OpportunityAnalyser

    r = OpportunityAnalyser(conn, engine).analyse(plan, as_of=as_of)

    print(f"\n  Opportunity cost")
    print(f"  {'─' * 76}")
    print(f"    Tax avoided now              {fmt(r.tax_avoided_now):>14}")
    if r.future_tax_saved_pv:
        print(f"    Future tax saved (PV)        {fmt(r.future_tax_saved_pv):>14}")
    print(f"    Transaction costs            {fmt(-r.transaction_costs):>14}")
    print(f"    {'─' * 44}")
    print(f"    Net benefit                  {fmt(r.net_benefit):>14}")

    if r.harvests:
        print(f"\n  Per harvest:")
        for h in r.harvests:
            print(f"    {h.name[:34]:<34} net {fmt(h.net_benefit):>11}")
            print(f"      {h.verdict}")
            if h.costs.breakdown():
                items = ", ".join(f"{k} {fmt(v)}" for k, v in h.costs.breakdown().items())
                print(f"      Costs: {items}")

    if r.deferrals:
        print(f"\n  Is waiting worth it?")
        for d in r.deferrals:
            print(f"    {d.name[:34]:<34} saves {fmt(d.tax_saved):>10} "
                  f"({d.breakeven_decline_pct:.1f}% of position)")
            print(f"      {d.verdict}")

    for n in r.notes:
        print(f"\n    · {n}")


def cmd_import_cas(args) -> int:
    from .ingest import cas

    conn = connect(args.db)
    try:
        statement = cas.parse(args.path, args.password)
    except ValueError as exc:
        print(f"Could not read the CAS: {exc}", file=sys.stderr)
        return 2

    print(f"\n  CAS {statement.period_from or '?'} to {statement.period_to or '?'}")
    print(f"  {len(statement.schemes)} scheme(s), {statement.transaction_count} transaction(s)\n")

    for s in statement.schemes:
        mark = "✓" if s.reconciles and s.is_complete else ("~" if s.reconciles else "✗")
        print(f"   {mark} {s.name[:46]:<46} {len(s.transactions):>3} txn  "
              f"closing {s.closing_balance if s.closing_balance is not None else '?'}")
        if not s.reconciles:
            print(f"       computed {s.computed_balance:.3f}, statement says "
                  f"{s.closing_balance}, off by {s.discrepancy:+.3f} units")
        elif not s.is_complete:
            print(f"       opens with {s.opening_balance:.3f} units carried in from before "
                  f"{statement.period_from or 'the statement'} — cost basis unknown")

    result = cas.import_statement(
        conn, statement, dry_run=args.dry_run, allow_partial=args.allow_partial
    )
    print(f"\n  {result.transactions_added} transaction(s) "
          f"{'would be added' if args.dry_run else 'added'}, "
          f"{result.duplicates_skipped} duplicate(s) skipped, "
          f"{result.schemes_created} new scheme(s).")

    for w in statement.warnings:
        print(f"  ⚠  {w}")
    for n in result.notes:
        print(f"  · {n}")
    if not args.dry_run and result.transactions_added:
        stats = rebuild(conn)
        print(f"  Rebuilt {stats['lots']} lots, {stats['disposals']} disposals.")
    print()
    return 1 if result.rejected else 0


def cmd_import_tradebook(args) -> int:
    from .ingest import tradebook

    conn = connect(args.db)
    parsed = tradebook.parse_file(args.path)
    if not parsed.trades:
        for w in parsed.warnings:
            print(f"  ⚠  {w}", file=sys.stderr)
        return 2

    result = tradebook.import_trades(conn, parsed, dry_run=args.dry_run)
    print(f"\n  {len(parsed.trades)} trade(s) parsed.")
    print(f"  {result.transactions_added} {'would be added' if args.dry_run else 'added'}, "
          f"{result.duplicates_skipped} duplicate(s) skipped, "
          f"{result.instruments_created} new instrument(s).")
    for w in parsed.warnings:
        print(f"  ⚠  {w}")
    for n in result.notes:
        print(f"  · {n}")
    if not args.dry_run and result.transactions_added:
        stats = rebuild(conn)
        print(f"  Rebuilt {stats['lots']} lots, {stats['disposals']} disposals.")
    print()
    return 0


def cmd_sip(args) -> int:
    from .planner import cashflow

    conn = connect(args.db)
    if args.sip_command == "add":
        cashflow.add_plan(
            conn, args.amount, label=args.label, day_of_month=args.day,
            start_date=args.start, step_up_pct=args.step_up,
            step_up_cap_rupees=args.cap,
        )
        print(f"Added {args.label}: {fmt(to_paise(args.amount))}/month on day {args.day}"
              + (f", stepping up {args.step_up}% a year" if args.step_up else ""))
        return 0

    fy = args.fy or financial_year(args.as_of or Date.today().isoformat())
    flow = cashflow.fy_cashflow(conn, fy, as_of=args.as_of)
    print(f"\n  SIP cashflow — FY {fy}")
    print(f"  {'─' * 60}")
    print(f"    Planned this year   {fmt(flow.planned):>16}")
    print(f"    Invested so far     {fmt(flow.actual):>16}  ({flow.completion_pct:.0f}%)")
    print(f"    Remaining scheduled {fmt(flow.remaining_scheduled):>16}")
    if flow.behind_by:
        print(f"    Behind plan by      {fmt(flow.behind_by):>16}  ← instalments due but unpaid")
    if flow.by_instrument:
        print(f"\n  By destination:")
        for name, amt in sorted(flow.by_instrument.items(), key=lambda kv: -kv[1]):
            print(f"    {name[:40]:<40} {fmt(amt):>14}")

    plans = cashflow.load_plans(conn)
    if plans and args.project:
        print(f"\n  Projected contribution over {args.project} years:")
        for year, amount in cashflow.project(plans, args.project):
            print(f"    {year}   {fmt(amount):>14}")
        total = sum(a for _, a in cashflow.project(plans, args.project))
        print(f"    {'Total':<9} {fmt(total):>14}")

    for n in flow.notes:
        print(f"\n  · {n}")
    print()
    return 0


def cmd_rules(args) -> int:
    from .rules import backtest as bt_mod
    from .rules.context import FIELDS
    from .rules.engine import RulesEngine, describe, load_rules

    conn = connect(args.db)
    as_of = args.as_of or Date.today().isoformat()
    engine = TaxEngine(args.fy or financial_year(as_of))

    if args.rules_command == "fields":
        print("\n  Fields available in rule conditions and messages\n")
        for name, meaning in FIELDS.items():
            print(f"    {name:<22} {meaning}")
        print()
        return 0

    if args.rules_command == "list":
        rules, warnings = load_rules()
        print(f"\n  {len(rules)} rule(s) in {bt_mod.__name__.rsplit('.', 1)[0]}/rules.yaml\n")
        for r in describe(rules):
            state = "" if r["enabled"] else "  (disabled)"
            print(f"    {r['name']:<28} [{r['severity']:<6}] {r['condition']}{state}")
            if r["scope"] != "all":
                print(f"      scope {r['scope']}, cooldown {r['cooldown_days']}d")
        for w in warnings:
            print(f"\n  ⚠  {w}")
        print()
        return 1 if warnings else 0

    if args.rules_command == "backtest":
        rules, _ = load_rules()
        matches = [r for r in rules if r.name == args.rule]
        if not matches:
            print(f"No rule named {args.rule!r}. Try `fpa rules list`.", file=sys.stderr)
            return 2
        rule = matches[0]
        result = bt_mod.Backtester(conn, engine).run(
            rule, end=as_of, step_days=args.step, start=args.start
        )
        print(f"\n  Backtest — {rule.name}")
        print(f"  {'─' * 74}")
        print(f"  Condition: {result.condition}")
        print(f"  {result.start} to {result.end}, every {result.step_days} days, "
              f"{result.dates_tested:,} evaluations\n")
        print(f"    {'Horizon':<10}{'After firing':>14}{'Base rate':>12}{'Edge':>10}")
        for h in result.base_rate:
            med, base, edge = result.median_forward(h), result.base_rate[h], result.edge(h)
            print(f"    {str(h) + 'd':<10}{_fmt_pct(med):>14}{_fmt_pct(base):>12}"
                  f"{_fmt_pct(edge, signed=True):>10}")
        print(f"\n  {result.verdict(90)}")
        if result.firings and args.show:
            print(f"\n  Firings ({min(args.show, result.count)} of {result.count}):")
            for f in result.firings[: args.show]:
                fwd = f.forward.get(90)
                print(f"    {f.date}  {f.subject[:30]:<30} "
                      f"90d after: {_fmt_pct(fwd):>8}   {f.context}")
        for n in result.notes:
            print(f"\n  · {n}")
        print()
        return 0

    # default: check
    rules_engine = RulesEngine(conn, tax_engine=engine)
    result = rules_engine.run(as_of, persist=not args.dry_run)

    print(f"\n  Rules — {as_of}")
    print(f"  {'─' * 74}")
    print(f"  {result.rules_evaluated} rule(s) against {result.subjects_evaluated} subject(s)")
    if not result.fired:
        print("\n  Nothing fired.")
    for severity in ("high", "medium", "info"):
        group = [a for a in result.fired if a.severity == severity]
        if not group:
            continue
        print(f"\n  {severity.upper()}")
        for a in group:
            print(f"    {a.subject[:34]:<34} {a.message}")
            if args.why:
                facts = {k: v for k, v in a.context.items() if not k.startswith("__")}
                print(f"      because: {facts}")

    if result.suppressed_cooldown or result.already_open:
        print(f"\n  {result.suppressed_cooldown} suppressed by cooldown, "
              f"{result.already_open} already open.")
    if args.dry_run:
        print("  Dry run — no alerts were saved.")
    for w in result.warnings:
        print(f"  ⚠  {w}")
    print()
    return 0


def cmd_alerts(args) -> int:
    from .rules.engine import RulesEngine

    conn = connect(args.db)
    engine = RulesEngine(conn)

    if args.alert_id and args.status:
        engine.set_status(args.alert_id, args.status.upper())
        print(f"Alert {args.alert_id} marked {args.status.upper()}.")
        return 0

    alerts = engine.open_alerts(include_muted=args.all)
    if not alerts:
        print("\n  No open alerts.\n")
        return 0
    print(f"\n  {len(alerts)} open alert(s)\n")
    for a in alerts:
        print(f"    #{a.id:<4} [{a.severity:<6}] {a.fired_on}  {a.subject[:28]:<28} {a.status}")
        print(f"          {a.message}")
    print("\n  Mark one:  fpa alerts <id> --status acked|acted|muted\n")
    return 0


def _fmt_pct(value: float | None, *, signed: bool = False) -> str:
    if value is None:
        return "—"
    return f"{value:+.1f}%" if signed else f"{value:.1f}%"


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
    s.add_argument("--economics", action="store_true",
                   help="show what the tax saving costs: charges, gap risk, breakeven moves")
    s.add_argument("--fy", default=None)
    s.add_argument("--as-of", default=None)
    s.set_defaults(func=cmd_plan)

    s = sub.add_parser("import-cas", help="import a CAMS/KFintech CAS PDF")
    s.add_argument("path")
    s.add_argument("--password", default=None,
                   help="CAS password (often your PAN in capitals)")
    s.add_argument("--dry-run", action="store_true",
                   help="parse and reconcile without writing — run this first")
    s.add_argument("--allow-partial", action="store_true",
                   help="import schemes whose statement starts mid-history, accepting that "
                        "their opening units have no cost basis")
    s.set_defaults(func=cmd_import_cas)

    s = sub.add_parser("import-tradebook", help="import a broker tradebook CSV")
    s.add_argument("path")
    s.add_argument("--dry-run", action="store_true")
    s.set_defaults(func=cmd_import_tradebook)

    s = sub.add_parser("sip", help="SIP schedule and financial-year cashflow")
    sip_sub = s.add_subparsers(dest="sip_command")
    s.add_argument("--fy", default=None)
    s.add_argument("--as-of", default=None)
    s.add_argument("--project", type=int, default=0, metavar="YEARS",
                   help="project total contribution over N years")
    s.set_defaults(func=cmd_sip, sip_command=None)

    a = sip_sub.add_parser("add", help="add a SIP plan")
    a.add_argument("amount", type=float, help="monthly instalment in rupees")
    a.add_argument("--label", default="SIP")
    a.add_argument("--day", type=int, default=5, help="day of month")
    a.add_argument("--start", default=None)
    a.add_argument("--step-up", type=float, default=0.0,
                   help="annual step-up percent, applied on each anniversary")
    a.add_argument("--cap", type=float, default=None, help="ceiling in rupees")
    a.set_defaults(func=cmd_sip, sip_command="add", fy=None, as_of=None, project=0)

    s = sub.add_parser("rules", help="evaluate, inspect and backtest your exit rules")
    rules_sub = s.add_subparsers(dest="rules_command")
    s.add_argument("--as-of", default=None)
    s.add_argument("--fy", default=None)
    s.add_argument("--dry-run", action="store_true", help="evaluate without saving alerts")
    s.add_argument("--why", action="store_true", help="show the facts that fired each rule")
    s.set_defaults(func=cmd_rules, rules_command="check")

    for name, helptext in (("check", "evaluate all rules now"),
                           ("list", "show the configured rules"),
                           ("fields", "list fields usable in conditions")):
        sp = rules_sub.add_parser(name, help=helptext)
        sp.add_argument("--as-of", default=None)
        sp.add_argument("--fy", default=None)
        sp.add_argument("--dry-run", action="store_true")
        sp.add_argument("--why", action="store_true")
        sp.set_defaults(func=cmd_rules, rules_command=name)

    sp = rules_sub.add_parser("backtest", help="replay a rule over history")
    sp.add_argument("rule")
    sp.add_argument("--start", default=None)
    sp.add_argument("--as-of", default=None)
    sp.add_argument("--fy", default=None)
    sp.add_argument("--step", type=int, default=7, help="days between evaluations")
    sp.add_argument("--show", type=int, default=0, help="print the first N firings")
    sp.set_defaults(func=cmd_rules, rules_command="backtest", dry_run=False, why=False)

    s = sub.add_parser("alerts", help="list open alerts or change one's status")
    s.add_argument("alert_id", nargs="?", type=int, default=None)
    s.add_argument("--status", choices=["acked", "acted", "muted", "new"], default=None)
    s.add_argument("--all", action="store_true", help="include muted and closed")
    s.set_defaults(func=cmd_alerts)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
