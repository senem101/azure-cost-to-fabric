"""Build a self-contained HTML preview of the Power BI cost report from local Gold tables.

The visuals match the report described in docs/05-power-bi-rapor.md, so you can
show stakeholders what Fabric will deliver before any cloud resources exist.

Usage:
    python build_report.py --gold ./output/gold --out ./output/cost_report.html
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

TEMPLATE = """<!doctype html>
<html lang="tr"><head><meta charset="utf-8">
<title>Azure Cost Report (local preview)</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
<style>
 body{font-family:Segoe UI,Arial,sans-serif;margin:0;background:#f4f6f9;color:#1f2933}
 header{background:#0b2a4a;color:#fff;padding:18px 28px}
 header h1{margin:0;font-size:22px} header p{margin:4px 0 0;opacity:.8;font-size:13px}
 .wrap{padding:20px 28px}
 .kpis{display:grid;grid-template-columns:repeat(6,1fr);gap:14px;margin-bottom:18px}
 .kpi{background:#fff;border-radius:8px;padding:14px 16px;box-shadow:0 1px 3px rgba(0,0,0,.08)}
 .kpi .l{font-size:12px;color:#52606d;text-transform:uppercase;letter-spacing:.04em}
 .kpi .v{font-size:24px;font-weight:600;margin-top:6px}
 .up{color:#c0392b}.down{color:#1e8449}
 .grid{display:grid;grid-template-columns:2fr 1fr;gap:14px;margin-bottom:14px}
 .card{background:#fff;border-radius:8px;padding:14px 16px;box-shadow:0 1px 3px rgba(0,0,0,.08)}
 .card h3{margin:0 0 10px;font-size:14px;color:#334e68}
 table{width:100%;border-collapse:collapse;font-size:13px}
 th,td{text-align:left;padding:6px 8px;border-bottom:1px solid #e4e7eb} td.n{text-align:right}
 canvas{max-height:320px}
</style></head><body>
<header><h1>Azure Cost Report</h1><p>__SUBTITLE__</p></header>
<div class="wrap">
 <div class="kpis">__KPIS__</div>
 <div class="grid"><div class="card"><h3>Daily effective cost by service category</h3><canvas id="daily"></canvas></div>
  <div class="card"><h3>Cost by resource group (period)</h3><canvas id="rg"></canvas></div></div>
 <div class="grid"><div class="card"><h3>Monthly effective cost by service</h3><canvas id="monthly"></canvas></div>
  <div class="card"><h3>Cost by environment tag</h3><canvas id="env"></canvas></div></div>
 <div class="grid"><div class="card"><h3>Top 10 resources</h3>__TOP__</div>
  <div class="card"><h3>Month-over-month by service (__LASTMONTH__)</h3>__MOM__</div></div>
</div>
<script>
const D=__DATA__;
const palette=['#0b6cbf','#f39c12','#27ae60','#8e44ad','#c0392b','#16a085','#2c3e50','#d35400','#7f8c8d','#e84393','#00b894','#6c5ce7'];
const ds=(series)=>Object.entries(series).map(([k,v],i)=>({label:k,data:v,backgroundColor:palette[i%palette.length],borderColor:palette[i%palette.length],fill:false,tension:.25}));
new Chart(daily,{type:'line',data:{labels:D.daily.labels,datasets:ds(D.daily.series)},options:{plugins:{legend:{position:'bottom'}},scales:{y:{beginAtZero:true}}}});
new Chart(monthly,{type:'bar',data:{labels:D.monthly.labels,datasets:ds(D.monthly.series)},options:{plugins:{legend:{position:'bottom'}},scales:{x:{stacked:true},y:{stacked:true}}}});
new Chart(rg,{type:'doughnut',data:{labels:D.rg.labels,datasets:[{data:D.rg.values,backgroundColor:palette}]},options:{plugins:{legend:{position:'right'}}}});
new Chart(env,{type:'bar',data:{labels:D.env.labels,datasets:[{label:'Effective cost',data:D.env.values,backgroundColor:'#0b6cbf'}]},options:{indexAxis:'y',plugins:{legend:{display:false}}}});
</script></body></html>"""


def money(v: float, cur: str) -> str:
    return f"{v:,.2f} {cur}"


def main() -> None:
    here = Path(__file__).parent
    parser = argparse.ArgumentParser()
    parser.add_argument("--gold", default=str(here / "output" / "gold"))
    parser.add_argument("--out", default=str(here / "output" / "cost_report.html"))
    args = parser.parse_args()
    g = Path(args.gold)

    fact = pd.read_parquet(g / "gold_fact_cost_daily.parquet")
    res = pd.read_parquet(g / "gold_dim_resource.parquet")
    svc = pd.read_parquet(g / "gold_dim_service.parquet")
    monthly = pd.read_parquet(g / "gold_cost_monthly.parquet")

    df = fact.merge(res, on="resource_key", how="left").merge(svc, on="service_name", how="left")
    df["charge_date"] = pd.to_datetime(df["charge_date"])
    df["year_month"] = df["charge_date"].dt.strftime("%Y-%m")
    cur = df["billing_currency"].dropna().iloc[0] if df["billing_currency"].notna().any() else ""

    months = sorted(df["year_month"].unique())
    current_month = months[-1]
    last_full = months[-2] if len(months) > 1 else months[-1]
    prev_full = months[-3] if len(months) > 2 else None
    lf_cost = df.loc[df.year_month == last_full, "effective_cost"].sum()
    pf_cost = df.loc[df.year_month == prev_full, "effective_cost"].sum() if prev_full else 0
    mom = (lf_cost - pf_cost) / pf_cost if pf_cost else 0
    mtd = df.loc[df.year_month == current_month, "effective_cost"].sum()

    kpis = [
        ("Total effective cost", money(df.effective_cost.sum(), cur), ""),
        (f"Last full month ({last_full})", money(lf_cost, cur), ""),
        ("MoM change", f"{mom:+.1%}", "up" if mom > 0 else "down"),
        (f"Month to date ({current_month})", money(mtd, cur), ""),
        ("Savings vs list", money(df.savings.sum(), cur), "down"),
        ("Services / resources", f"{df.service_name.nunique()} / {res[res.resource_key != 'unassigned'].shape[0]}", ""),
    ]
    kpi_html = "".join(f'<div class="kpi"><div class="l">{l}</div><div class="v {c}">{v}</div></div>' for l, v, c in kpis)

    daily = df.pivot_table(index="charge_date", columns="service_category", values="effective_cost", aggfunc="sum").fillna(0)
    mon = df.pivot_table(index="year_month", columns="service_name", values="effective_cost", aggfunc="sum").fillna(0)
    rg = df.groupby("resource_group")["effective_cost"].sum().sort_values(ascending=False)
    env = df.groupby("tag_environment")["effective_cost"].sum().sort_values(ascending=False)

    top = (
        df[df.resource_key != "unassigned"]
        .groupby(["resource_name", "resource_group", "service_name"])["effective_cost"].sum()
        .sort_values(ascending=False).head(10).reset_index()
    )
    top_html = "<table><tr><th>Resource</th><th>Resource group</th><th>Service</th><th>Cost</th></tr>" + "".join(
        f"<tr><td>{r.resource_name}</td><td>{r.resource_group}</td><td>{r.service_name}</td><td class='n'>{money(r.effective_cost, cur)}</td></tr>"
        for r in top.itertuples()
    ) + "</table>"

    lm = monthly[monthly.year_month == last_full].sort_values("effective_cost", ascending=False)
    mom_html = "<table><tr><th>Service</th><th>Cost</th><th>Prev.</th><th>MoM</th></tr>" + "".join(
        f"<tr><td>{r.service_name}</td><td class='n'>{money(r.effective_cost, cur)}</td>"
        f"<td class='n'>{'' if pd.isna(r.previous_month_cost) else money(r.previous_month_cost, cur)}</td>"
        f"<td class='n {'up' if (r.mom_change_pct or 0) > 0 else 'down'}'>{'' if pd.isna(r.mom_change_pct) else f'{r.mom_change_pct:+.1%}'}</td></tr>"
        for r in lm.itertuples()
    ) + "</table>"

    data = {
        "daily": {"labels": [d.strftime("%Y-%m-%d") for d in daily.index],
                  "series": {c: daily[c].round(2).tolist() for c in daily.columns}},
        "monthly": {"labels": list(mon.index), "series": {c: mon[c].round(2).tolist() for c in mon.columns}},
        "rg": {"labels": list(rg.index), "values": rg.round(2).tolist()},
        "env": {"labels": list(env.index), "values": env.round(2).tolist()},
    }

    html = (
        TEMPLATE.replace("__DATA__", json.dumps(data))
        .replace("__KPIS__", kpi_html)
        .replace("__TOP__", top_html)
        .replace("__MOM__", mom_html)
        .replace("__LASTMONTH__", last_full)
        .replace("__SUBTITLE__", f"{months[0]} → {current_month} · source: Gold tables (local demo) · currency {cur}")
    )
    Path(args.out).write_text(html, encoding="utf-8")
    print(f"Report written to {Path(args.out).resolve()}")


if __name__ == "__main__":
    main()
