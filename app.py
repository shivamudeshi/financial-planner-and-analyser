"""Dashboard.  Run with:  streamlit run app.py"""

from __future__ import annotations

from datetime import date as Date

import pandas as pd
import streamlit as st

from fpa.analysis import technicals
from fpa.analysis.returns import portfolio_xirr
from fpa.db import DEFAULT_DB, connect, financial_year, fy_bounds
from fpa.ingest.equity_prices import staleness
from fpa.lots import brought_forward, realised_gains
from fpa.money import fmt, fmt_compact, to_rupees
from fpa.planner import cashflow
from fpa.planner.opportunity import OpportunityAnalyser
from fpa.planner.sell_planner import Mode, SellPlanner
from fpa.portfolio import positions
from fpa.rules.backtest import Backtester
from fpa.rules.engine import RulesEngine, describe
from fpa.tax.engine import TaxEngine, Term

st.set_page_config(page_title="Financial Planner & Analyser", page_icon="📊", layout="wide")


@st.cache_resource
def get_conn():
    # Streamlit reruns the script on a fresh thread each interaction while this
    # cached connection survives, so the same-thread guard has to be relaxed.
    return connect(DEFAULT_DB, same_thread=False)


conn = get_conn()

if not conn.execute("SELECT COUNT(*) FROM instruments").fetchone()[0]:
    st.title("No data yet")
    st.write(
        "The ledger is empty. Load a sample portfolio to explore the app, or import "
        "your own transactions."
    )
    st.code("python -m fpa.cli sample     # generate a sample portfolio\n"
            "python -m fpa.cli rebuild    # rebuild lots from transactions", language="bash")
    st.stop()

# ---------------------------------------------------------------- sidebar

st.sidebar.title("📊 Planner")
as_of = st.sidebar.date_input("As of", value=Date.today()).isoformat()
fy = st.sidebar.selectbox(
    "Financial year",
    options=sorted({financial_year(as_of), *(r["fy"] for r in conn.execute(
        "SELECT DISTINCT fy FROM disposals"))}, reverse=True),
)
engine = TaxEngine(fy)
page = st.sidebar.radio(
    "View",
    ["Alerts", "Sell planner", "Overview", "Holdings", "Cashflow", "Tax", "Analysis"],
)

# `or` would be wrong here: days_stale of 0 is the freshest possible value, not
# a missing one. Only None (an instrument with no prices at all) means unknown.
stale = [
    s for s in staleness(conn, as_of)
    if s["days_stale"] is None or s["days_stale"] > 5
]
if stale:
    st.sidebar.warning(f"{len(stale)} instrument(s) have prices older than 5 days.")

pos = positions(conn, as_of)
total_value = sum(p.market_value for p in pos)
total_cost = sum(p.cost for p in pos)


# ---------------------------------------------------------------- planner

def render_planner():
    st.title("Sell planner")
    st.caption(
        f"The largest set of sales that keeps FY {fy} tax at exactly zero. "
        "Nothing here places an order."
    )

    c1, c2, c3 = st.columns([1, 1, 2])
    mode = Mode[c1.radio("Objective", ["EXIT", "HARVEST"], horizontal=True,
                         help="EXIT frees the most capital from positions you want out of. "
                              "HARVEST realises gains tax-free and rebuys, to step up cost basis.")]
    min_priority = c2.slider("Minimum exit priority", 0, 100, 0, step=5,
                             help="Only consider holdings you have flagged at or above this "
                                  "conviction-to-exit score.")
    book_losses = c3.checkbox("Allow booking losses", value=True)

    plan = SellPlanner(conn, engine, fy=fy, as_of=as_of).plan(
        mode, min_priority=min_priority, book_losses=book_losses
    )

    m = st.columns(4)
    m[0].metric("Capital freed, tax-free", fmt_compact(plan.proceeds))
    m[1].metric("Gain realised", fmt_compact(plan.gain_realised))
    m[2].metric("Tax payable", fmt(plan.tax))
    m[3].metric("Tax avoided", fmt_compact(plan.tax_saved),
                help="Versus selling these same positions outright today.")

    used = plan.final.exemption_used
    limit = plan.final.exemption_limit
    st.progress(min(1.0, used / limit) if limit else 0.0,
                text=f"s.112A exemption used: {fmt(used)} of {fmt(limit)} "
                     f"— {fmt(plan.exemption_left)} expires on {fy_bounds(fy)[1]}")

    orders = plan.by_instrument()
    if orders:
        st.subheader("Place these orders")
        st.caption(
            "One row per order — your broker or AMC applies FIFO across lots itself. "
            "Expand a row for the lot detail behind it."
        )
        st.dataframe(
            pd.DataFrame([{
                "Instrument": ip.name,
                "Symbol": ip.symbol or "—",
                "Action": "Redeem" if ip.kind == "MF" else "Sell",
                "Qty": round(ip.quantity, 3),
                "Proceeds": float(to_rupees(ip.proceeds)),
                "Gain": float(to_rupees(ip.gain)),
                "Gain %": round(ip.gain_pct, 1),
                "Lots": ip.lot_count,
                "Full exit": "Yes" if ip.is_full_exit else "Partial",
            } for ip in orders]),
            hide_index=True, use_container_width=True,
        )
        for ip in orders:
            with st.expander(f"{ip.name} — {ip.summary()}"):
                st.dataframe(
                    pd.DataFrame([{
                        "Bought": a.buy_date,
                        "Qty": round(a.quantity, 3),
                        "Price": float(to_rupees(a.price)),
                        "Proceeds": float(to_rupees(a.sale_value)),
                        "Gain": float(to_rupees(a.gain)),
                        "Term": "Long" if a.term is Term.LONG else "Short",
                        "Held": f"{a.days_held}d",
                        "Why": a.rationale,
                    } for a in sorted(ip.actions, key=lambda a: a.buy_date)]),
                    hide_index=True, use_container_width=True,
                )
    else:
        st.info("No sales are possible at zero tax under these settings.")

    deferrals = plan.deferrals_by_instrument()
    if deferrals:
        st.subheader("Wait, don't sell")
        st.caption(
            "Short-term gains that become long-term — and much cheaper — by holding on. "
            "Dated to the last lot that crosses 12 months."
        )
        st.dataframe(
            pd.DataFrame([{
                "Instrument": d["name"],
                "Qty": round(d["quantity"], 3),
                "Unrealised gain": float(to_rupees(d["gain"])),
                "Lots": d["lots"],
                "Fully long-term in": f"{d['days_to_long_term']}d",
                "From": d["long_term_date"],
                "Tax if sold now": float(to_rupees(d["tax_if_sold_now"])),
                "Crosses within this FY": "Yes" if d["within_this_fy"] else "No",
            } for d in deferrals]),
            hide_index=True, use_container_width=True,
        )

    render_economics(plan)

    for w in plan.warnings:
        st.warning(w)
    with st.expander("How this plan was built", expanded=not plan.actions):
        for n in plan.notes:
            st.markdown(f"- {n}")


def render_economics(plan):
    """What the tax saving costs. The counterweight to the plan above."""
    st.divider()
    st.subheader("Opportunity cost")
    st.caption(
        "Tax is not the objective. This is what achieving the plan above actually costs, "
        "and whether the saving is large or small next to the price risk you take on."
    )

    report = OpportunityAnalyser(conn, engine).analyse(plan, as_of=as_of)

    m = st.columns(4)
    m[0].metric("Tax avoided now", fmt_compact(report.tax_avoided_now))
    m[1].metric("Future tax saved (PV)", fmt_compact(report.future_tax_saved_pv),
                help="Discounted value of tax escaped later thanks to a higher cost basis.")
    m[2].metric("Transaction costs", fmt_compact(-report.transaction_costs),
                help="Brokerage, STT, stamp duty, GST, DP charges and exit load.")
    m[3].metric("Net benefit", fmt_compact(report.net_benefit),
                delta="worth doing" if report.net_benefit > 0 else "not worth it",
                delta_color="normal" if report.net_benefit > 0 else "inverse")

    if report.harvests:
        st.markdown("**Is each harvest worth doing?**")
        st.dataframe(
            pd.DataFrame([{
                "Instrument": h.name,
                "Turnover": float(to_rupees(h.value)),
                "Future tax saved (PV)": float(to_rupees(h.future_tax_saved_pv)),
                "Round-trip cost": float(to_rupees(h.round_trip_cost)),
                "Days out of market": h.gap_days,
                "Gap risk (1σ)": float(to_rupees(h.gap_risk)),
                "Net": float(to_rupees(h.net_benefit)),
                "Verdict": h.verdict,
            } for h in report.harvests]),
            hide_index=True, use_container_width=True,
        )

    if report.deferrals:
        st.markdown("**Is waiting for long-term worth the price risk?**")
        st.caption(
            "Breakeven is the price fall that exactly cancels the tax saved. When volatility "
            "over the wait dwarfs it, the tax is not what should drive the decision."
        )
        st.dataframe(
            pd.DataFrame([{
                "Instrument": d.name,
                "Wait": f"{d.days_to_wait}d",
                "Tax saved": float(to_rupees(d.tax_saved)),
                "Breakeven fall %": round(d.breakeven_decline_pct, 2),
                "Volatility over wait %": round(d.volatility_pct, 1) if d.volatility_pct else None,
                "Risk ÷ reward": round(d.risk_multiple, 1) if d.risk_multiple else None,
                "Verdict": d.verdict,
            } for d in report.deferrals]),
            hide_index=True, use_container_width=True,
        )

    with st.expander("Assumptions behind these numbers"):
        for n in report.notes:
            st.markdown(f"- {n}")


# ---------------------------------------------------------------- overview

def render_overview():
    st.title("Overview")
    gain = total_value - total_cost
    x = portfolio_xirr(conn, as_of=as_of)

    m = st.columns(4)
    m[0].metric("Portfolio value", fmt_compact(total_value))
    m[1].metric("Invested", fmt_compact(total_cost))
    m[2].metric("Unrealised gain", fmt_compact(gain),
                delta=f"{(gain / total_cost * 100) if total_cost else 0:.1f}%")
    m[3].metric("XIRR", f"{x * 100:.1f}%" if x is not None else "—",
                help="Money-weighted return on your actual cashflows, not a point-to-point figure.")

    c1, c2 = st.columns(2)
    with c1:
        st.subheader("Allocation vs target")
        actual: dict[str, int] = {}
        for p in pos:
            actual[p.asset_class] = actual.get(p.asset_class, 0) + p.market_value
        targets = {r["asset_class"]: r["target_pct"]
                   for r in conn.execute("SELECT * FROM plan_targets WHERE fy=?", (fy,))}
        rows = [{
            "Asset class": k,
            "Value": float(to_rupees(v)),
            "Actual %": round(100 * v / total_value, 1) if total_value else 0,
            "Target %": targets.get(k),
            "Drift": round((100 * v / total_value) - targets[k], 1) if k in targets and total_value else None,
        } for k, v in sorted(actual.items(), key=lambda kv: -kv[1])]
        st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)

    with c2:
        st.subheader("Unrealised gain by tax term")
        short = sum(p.unrealised_by_term(engine)[Term.SHORT] for p in pos)
        long = sum(p.unrealised_by_term(engine)[Term.LONG] for p in pos)
        st.dataframe(pd.DataFrame([
            {"Term": "Short-term (≤12m)", "Unrealised gain": float(to_rupees(short)),
             "Rate if sold now": f"{engine.equity_stcg_rate:.0%}"},
            {"Term": "Long-term (>12m)", "Unrealised gain": float(to_rupees(long)),
             "Rate if sold now": f"{engine.equity_ltcg_rate:.1%} after ₹1.25L exemption"},
        ]), hide_index=True, use_container_width=True)

    st.subheader("Largest positions")
    st.dataframe(
        pd.DataFrame([{
            "Instrument": p.name, "Type": p.kind, "Value": float(to_rupees(p.market_value)),
            "Gain %": round(p.gain_pct, 1), "Weight %": round(100 * p.market_value / total_value, 1)
            if total_value else 0, "Exit priority": p.exit_priority,
        } for p in pos[:15]]),
        hide_index=True, use_container_width=True,
    )


# ---------------------------------------------------------------- holdings

def render_holdings():
    st.title("Holdings")
    st.caption("Exit priority drives the planner: 0 means never sell, 100 means get me out.")

    for p in pos:
        with st.expander(
            f"{p.name} — {fmt_compact(p.market_value)} ({p.gain_pct:+.1f}%)"
        ):
            c = st.columns(4)
            c[0].metric("Quantity", f"{p.quantity:,.3f}".rstrip("0").rstrip("."))
            c[1].metric("Cost", fmt_compact(p.cost))
            c[2].metric("Unrealised", fmt_compact(p.gain))
            new_priority = c[3].number_input(
                "Exit priority", 0, 100, p.exit_priority, step=5, key=f"prio-{p.instrument_id}"
            )
            if new_priority != p.exit_priority:
                with conn:
                    conn.execute("UPDATE instruments SET exit_priority=? WHERE id=?",
                                 (new_priority, p.instrument_id))
                st.rerun()

            st.dataframe(
                pd.DataFrame([{
                    "Bought": l.lot.buy_date,
                    "Qty": round(l.quantity, 3),
                    "Cost/unit": float(to_rupees(l.lot.cost_per_unit)),
                    "Value": float(to_rupees(l.market_value)),
                    "Gain": float(to_rupees(l.gain)),
                    "Gain %": round(l.gain_pct, 1),
                    "Held": l.days_held,
                    "Term": "Long" if l.term(engine) is Term.LONG else "Short",
                    "Days to long-term": l.days_to_long_term(engine) or "—",
                } for l in sorted(p.lots, key=lambda l: l.lot.buy_date)]),
                hide_index=True, use_container_width=True,
            )


# ---------------------------------------------------------------- tax

def render_tax():
    st.title(f"Tax — FY {fy}")
    booked = realised_gains(conn, fy)
    bf_stcl, bf_ltcl = brought_forward(conn, fy)
    res = engine.compute(booked, bf_stcl=bf_stcl, bf_ltcl=bf_ltcl)

    m = st.columns(4)
    m[0].metric("Realised gain", fmt_compact(sum(g.amount for g in booked)))
    m[1].metric("Tax payable", fmt(res.total_tax))
    m[2].metric("Exemption used", fmt(res.exemption_used))
    m[3].metric("Exemption left", fmt(res.exemption_unused))

    st.subheader("By bucket")
    st.dataframe(pd.DataFrame([{
        "Bucket": b.key.replace("_", " ").title(),
        "Rate": f"{b.rate:.1%}",
        "Gross gain": float(to_rupees(b.gross)),
        "Losses set off": float(to_rupees(b.setoff)),
        "Exempt": float(to_rupees(b.exempt)),
        "Taxable": float(to_rupees(b.taxable)),
        "Tax": float(to_rupees(b.tax)),
    } for b in res.buckets if b.gross or b.setoff]), hide_index=True, use_container_width=True)

    if res.carry_forward_stcl or res.carry_forward_ltcl:
        st.subheader("Carried forward")
        st.write(
            f"Short-term loss {fmt(res.carry_forward_stcl)}, "
            f"long-term loss {fmt(res.carry_forward_ltcl)} — valid "
            f"{engine.carry_forward_years} assessment years, **only if the return is filed "
            f"by the s.139(1) due date**."
        )

    rows = conn.execute(
        "SELECT d.sell_date, i.name, d.quantity, d.sale_value, d.cost, d.gain, d.term, d.days_held"
        " FROM disposals d JOIN instruments i ON i.id=d.instrument_id WHERE d.fy=?"
        " ORDER BY d.sell_date DESC", (fy,)).fetchall()
    if rows:
        st.subheader("Realised this year")
        st.dataframe(pd.DataFrame([{
            "Date": r["sell_date"], "Instrument": r["name"], "Qty": round(r["quantity"], 3),
            "Proceeds": float(to_rupees(r["sale_value"])), "Cost": float(to_rupees(r["cost"])),
            "Gain": float(to_rupees(r["gain"])), "Term": r["term"].replace("_", " ").title(),
            "Held": f"{r['days_held']}d",
        } for r in rows]), hide_index=True, use_container_width=True)

    for n in res.notes:
        st.info(n)
    st.caption(
        f"Computed under FY {fy} parameters from fpa/tax/rates.yaml. "
        "Verify against the current Finance Act before filing or acting."
    )


# ---------------------------------------------------------------- analysis

def render_analysis():
    st.title("Analysis")
    equities = [p for p in pos if p.kind == "EQUITY"]
    funds = [p for p in pos if p.kind == "MF"]

    tab_eq, tab_mf = st.tabs(["Equities — technicals", "Funds — returns"])

    with tab_eq:
        if not equities:
            st.info("No equity holdings.")
        else:
            names = {p.name: p.instrument_id for p in equities}
            pick = st.selectbox("Instrument", list(names))
            iid = names[pick]
            snap = technicals.snapshot(conn, iid, as_of)
            if snap:
                c = st.columns(5)
                c[0].metric("Close", fmt(snap.close))
                c[1].metric("RSI(14)", f"{snap.rsi14:.0f}" if snap.rsi14 else "—")
                c[2].metric("Trend", snap.trend)
                c[3].metric("From 52w high", f"{snap.from_52w_high:.1f}%"
                            if snap.from_52w_high is not None else "—")
                c[4].metric("Above 200DMA", {True: "Yes", False: "No", None: "—"}[snap.above_200dma])

            df = technicals.price_frame(conn, iid, as_of)
            if not df.empty:
                close = df["close"].astype(float) / 100
                chart = pd.DataFrame({
                    "Close": close,
                    "50 DMA": technicals.sma(close, 50),
                    "200 DMA": technicals.sma(close, 200),
                })
                st.line_chart(chart)
            st.caption(
                "Indicators are inputs to your own exit rules, not signals. "
                "Nothing here predicts a price."
            )

    with tab_mf:
        if not funds:
            st.info("No fund holdings.")
        else:
            st.dataframe(pd.DataFrame([{
                "Fund": p.name,
                "Category": p.category,
                "Value": float(to_rupees(p.market_value)),
                "Invested": float(to_rupees(p.cost)),
                "Absolute %": round(p.gain_pct, 1),
                "XIRR %": round((portfolio_xirr(conn, p.instrument_id, as_of) or 0) * 100, 1),
            } for p in funds]), hide_index=True, use_container_width=True)
            st.caption(
                "XIRR is the money-weighted return on your actual SIP cashflows — the number "
                "that reflects what you earned. A fund's advertised return assumes a lump sum "
                "you never invested. No technical indicators are shown for funds: NAV has no "
                "volume or order flow, so momentum indicators on it are meaningless."
            )


def render_cashflow():
    st.title(f"Cashflow — FY {fy}")
    st.caption(
        "What goes in, alongside what the sell planner frees. The two are one pool of "
        "deployable money, and treating them separately is how people sell to fund something "
        "their SIP was already covering."
    )

    flow = cashflow.fy_cashflow(conn, fy, as_of=as_of)
    plans = cashflow.load_plans(conn)

    m = st.columns(4)
    m[0].metric("Planned this year", fmt_compact(flow.planned))
    m[1].metric("Invested so far", fmt_compact(flow.actual),
                delta=f"{flow.completion_pct:.0f}% of plan")
    m[2].metric("Remaining scheduled", fmt_compact(flow.remaining_scheduled))
    m[3].metric("Behind plan by", fmt_compact(flow.behind_by),
                help="Instalments already due but not invested. Excludes what isn't due yet.",
                delta_color="inverse")

    with st.expander("Add a SIP plan", expanded=not plans):
        c = st.columns(5)
        amount = c[0].number_input("Monthly ₹", 500, 10_00_000, 17_000, step=1_000)
        day = c[1].number_input("Day", 1, 28, 5)
        step_up = c[2].number_input("Step-up %/yr", 0.0, 50.0, 10.0, step=1.0,
                                    help="Applied on each anniversary of the start date, "
                                         "which is how AMCs implement it.")
        start = c[3].date_input("Starts", value=Date.fromisoformat(as_of))
        names = {"(whole portfolio)": None} | {p.name: p.instrument_id for p in pos}
        target = c[4].selectbox("Into", list(names))
        if st.button("Add plan"):
            cashflow.add_plan(conn, float(amount), instrument_id=names[target],
                              label=target if names[target] else "SIP",
                              day_of_month=int(day), start_date=start.isoformat(),
                              step_up_pct=float(step_up))
            st.rerun()

    if plans:
        st.subheader("Plans")
        st.dataframe(pd.DataFrame([{
            "Label": p.label,
            "Instalment now": float(to_rupees(p.amount)),
            "Day": p.day_of_month,
            "Starts": p.start_date,
            "Step-up %/yr": p.step_up_pct,
            "In 5 years": float(to_rupees(p.amount_in_year(5))),
            "In 10 years": float(to_rupees(p.amount_in_year(10))),
        } for p in plans]), hide_index=True, use_container_width=True)

        years = st.slider("Project contributions over", 1, 20, 10)
        rows = cashflow.project(plans, years)
        st.bar_chart(pd.DataFrame(
            {"Contribution": [float(to_rupees(a)) for _, a in rows]},
            index=[f for f, _ in rows],
        ))
        total = sum(a for _, a in rows)
        flat = sum(p.amount * 12 for p in plans) * years
        st.caption(
            f"Total contribution over {years} years: **{fmt(total)}**"
            + (f" — against {fmt(flat)} if the instalment never rose. "
               f"Contribution only, no assumed return."
               if total != flat else ". Contribution only, no assumed return.")
        )

    if flow.by_instrument:
        st.subheader("Where it goes")
        st.dataframe(pd.DataFrame([
            {"Destination": k, "This year": float(to_rupees(v))}
            for k, v in sorted(flow.by_instrument.items(), key=lambda kv: -kv[1])
        ]), hide_index=True, use_container_width=True)

    plan = SellPlanner(conn, engine, fy=fy, as_of=as_of).plan(Mode.EXIT)
    total, notes = cashflow.deployable(plan.proceeds, flow, as_of)
    st.subheader("Deployable this year")
    st.metric("Sale proceeds + remaining SIP", fmt_compact(total))
    for n in notes:
        st.markdown(f"- {n}")
    for n in flow.notes:
        st.info(n)


SEVERITY_ICON = {"high": "🔴", "medium": "🟠", "info": "🔵"}


def render_alerts():
    st.title("Alerts")
    st.caption(
        "Your own exit rules, evaluated against today's positions. Every alert records the "
        "facts that fired it — nothing here places an order."
    )

    rules_engine = RulesEngine(conn, tax_engine=engine)
    for w in rules_engine.warnings:
        st.error(w)

    c1, c2, c3 = st.columns([1, 1, 3])
    if c1.button("Run rules now", type="primary"):
        result = rules_engine.run(as_of)
        st.session_state["last_run"] = (
            f"{len(result.fired)} new alert(s); {result.already_open} already open, "
            f"{result.suppressed_cooldown} suppressed by cooldown."
        )
        st.rerun()
    show_all = c2.checkbox("Include closed")
    if msg := st.session_state.get("last_run"):
        c3.success(msg)

    alerts = rules_engine.open_alerts(include_muted=show_all)
    if not alerts:
        st.info("No open alerts. Run the rules to check against today's prices.")
    else:
        counts = {s: sum(1 for a in alerts if a.severity == s) for s in ("high", "medium", "info")}
        m = st.columns(3)
        for col, (sev, n) in zip(m, counts.items()):
            col.metric(f"{SEVERITY_ICON[sev]} {sev.title()}", n)

    for a in alerts:
        with st.container(border=True):
            top, actions = st.columns([5, 2])
            top.markdown(
                f"{SEVERITY_ICON.get(a.severity, '•')} **{a.subject}** — {a.message}  \n"
                f"<span style='opacity:.6;font-size:0.85em'>{a.rule_name} · fired "
                f"{a.fired_on} · {a.status}</span>",
                unsafe_allow_html=True,
            )
            b = actions.columns(3)
            for label, status in (("Ack", "ACKED"), ("Acted", "ACTED"), ("Mute", "MUTED")):
                if b[("Ack", "Acted", "Mute").index(label)].button(
                    label, key=f"{status}-{a.id}", disabled=a.status == status
                ):
                    rules_engine.set_status(a.id, status)
                    st.rerun()
            facts = {k: v for k, v in a.context.items() if not k.startswith("__")}
            if facts:
                with st.expander("Why this fired"):
                    st.write(facts)
                    st.code(a.context.get("__condition__", ""), language="python")

    st.divider()
    with st.expander("Configured rules"):
        st.caption(
            "Edit `fpa/rules/rules.yaml` to change these. Prefer few, simple, explainable "
            "rules — an alert list you stop reading is worse than no alerts."
        )
        st.dataframe(pd.DataFrame(describe(rules_engine.rules)), hide_index=True,
                     use_container_width=True)

    with st.expander("Does a rule actually work? (backtest)"):
        st.caption(
            "Replays a rule over your real history and compares what happened next against "
            "the base rate — the median forward return across every tested date. A rule earns "
            "its place by beating that, not by being right in a falling market."
        )
        names = [r.name for r in rules_engine.rules]
        pick = st.selectbox("Rule", names, key="bt-rule")
        step = st.slider("Evaluate every N days", 1, 30, 7)
        if st.button("Run backtest"):
            rule = next(r for r in rules_engine.rules if r.name == pick)
            with st.spinner("Replaying history…"):
                res = Backtester(conn, engine).run(rule, end=as_of, step_days=step)
            st.markdown(f"**{res.verdict(90)}**")
            if res.base_rate:
                st.dataframe(pd.DataFrame([{
                    "Horizon": f"{h}d",
                    "After firing %": round(res.median_forward(h), 2)
                    if res.median_forward(h) is not None else None,
                    "Base rate %": round(res.base_rate[h], 2)
                    if res.base_rate[h] is not None else None,
                    "Edge (pp)": round(res.edge(h), 2) if res.edge(h) is not None else None,
                    "Fell afterwards %": round(res.hit_rate(h), 0)
                    if res.hit_rate(h) is not None else None,
                } for h in res.base_rate]), hide_index=True, use_container_width=True)
            if res.firings:
                st.dataframe(pd.DataFrame([{
                    "Date": f.date, "Instrument": f.subject,
                    "90d after %": round(f.forward.get(90), 1)
                    if f.forward.get(90) is not None else None,
                    "Because": f.context,
                } for f in res.firings[:50]]), hide_index=True, use_container_width=True)
            for n in res.notes:
                st.info(n)


{"Alerts": render_alerts, "Sell planner": render_planner, "Overview": render_overview,
 "Holdings": render_holdings, "Cashflow": render_cashflow, "Tax": render_tax,
 "Analysis": render_analysis}[page]()
