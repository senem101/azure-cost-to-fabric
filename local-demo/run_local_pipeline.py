"""Run the Bronze -> Silver -> Gold cost pipeline locally with pandas.

This is a faithful, offline re-implementation of the three Fabric notebooks in
fabric/notebooks/. It reads the same export folder layout and produces the
same Gold tables, so you can understand (and demo) every transformation
without an Azure subscription or a Fabric capacity.

Usage:
    python run_local_pipeline.py --input ./sample-data --output ./output
"""

from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

EXPORT_PATH_RE = re.compile(r"^(.*)/\d{8}-\d{8}/")
PERIOD_RE = re.compile(r"/(\d{8}-\d{8})/")
RUN_RE = re.compile(r"/\d{8}-\d{8}/([^/]+)/")
RG_RE = re.compile(r"/resourcegroups/([^/]+)", re.IGNORECASE)
UNASSIGNED = "unassigned"
RUN_KEY = ["_export_path", "_billing_period"]


def banner(title: str) -> None:
    print(f"\n{'=' * 78}\n{title}\n{'=' * 78}")


def pick(df: pd.DataFrame, *names: str) -> pd.Series:
    """Case-insensitive coalesce over FOCUS column-name variants (same as the notebooks)."""
    by_lower = {c.lower(): c for c in df.columns}
    result = pd.Series([None] * len(df), index=df.index, dtype="object")
    for name in names:
        col = by_lower.get(name.lower())
        if col is not None:
            result = result.where(result.notna(), df[col])
    return result


# ─── Bronze ──────────────────────────────────────────────────────────────────

def bronze(input_dir: Path) -> pd.DataFrame:
    banner("BRONZE  - raw FOCUS parquet files + lineage columns")
    files = sorted((input_dir / "costs").rglob("*.parquet"))
    if not files:
        raise SystemExit(f"No parquet files under {input_dir / 'costs'}. Run generate_sample_focus.py first.")

    frames = []
    for f in files:
        part = pd.read_parquet(f)
        part["_source_file"] = f.as_posix()
        part["_file_modification_time"] = datetime.fromtimestamp(f.stat().st_mtime, timezone.utc)
        frames.append(part)
        print(f"  read {f.relative_to(input_dir).as_posix():<95} {len(part):>5} rows")

    df = pd.concat(frames, ignore_index=True)
    df["_ingestion_timestamp"] = datetime.now(timezone.utc)
    print(f"\n  bronze_costs: {len(df)} rows, {len(df.columns)} columns (all export runs, unchanged values)")
    return df


# ─── Silver ──────────────────────────────────────────────────────────────────

def keep_latest_export_run(df: pd.DataFrame) -> pd.DataFrame:
    """Keep the newest run per (export folder, billing period); each subscription has its own export folder."""
    df = df.copy()
    df["_export_path"] = df["_source_file"].map(lambda p: (EXPORT_PATH_RE.search(p) or [None, ""])[1])
    df["_billing_period"] = df["_source_file"].map(lambda p: (PERIOD_RE.search(p) or [None, ""])[1])
    df["_export_run"] = df["_source_file"].map(lambda p: (RUN_RE.search(p) or [None, ""])[1])

    runs = (
        df.groupby([*RUN_KEY, "_export_run"], as_index=False)["_file_modification_time"].max()
        .sort_values("_file_modification_time", ascending=False)
        .drop_duplicates(RUN_KEY)
    )
    run_counts = df.groupby(RUN_KEY)["_export_run"].nunique()
    print("  latest export run per export folder and billing period:")
    for _, r in runs.sort_values(RUN_KEY).iterrows():
        export = "/".join(r["_export_path"].split("/")[-2:])
        total = run_counts[(r["_export_path"], r["_billing_period"])]
        print(f"    {export:<55} {r['_billing_period']}  run {r['_export_run'][:8]}...  ({total} run(s) found)")

    naive = (
        df.groupby(["_billing_period", "_export_run"], as_index=False)["_file_modification_time"].max()
        .sort_values("_file_modification_time", ascending=False)
        .drop_duplicates("_billing_period")
    )
    naive_rows = len(df.merge(naive[["_billing_period", "_export_run"]], on=["_billing_period", "_export_run"]))
    kept_rows = len(df.merge(runs[[*RUN_KEY, "_export_run"]], on=[*RUN_KEY, "_export_run"]))
    if naive_rows != kept_rows:
        print(f"  note: keying on billing period alone would keep only {naive_rows} of {kept_rows} rows "
              f"(other subscriptions' exports would be dropped)")
    return df.merge(runs[[*RUN_KEY, "_export_run"]], on=[*RUN_KEY, "_export_run"])


def parse_tags(value) -> dict:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return {}
    if isinstance(value, dict):
        raw = value
    else:
        text = str(value).strip()
        if not text.startswith("{"):
            text = "{" + text + "}"  # some exports omit the outer braces
        try:
            raw = json.loads(text)
        except json.JSONDecodeError:
            return {}
    return {str(k).lower(): None if v is None else str(v) for k, v in raw.items()}


def silver(df_bronze: pd.DataFrame) -> pd.DataFrame:
    banner("SILVER  - latest run only, typed & conformed cost records")
    before = len(df_bronze)
    df = keep_latest_export_run(df_bronze)
    print(f"  rows: {before} -> {len(df)} after dropping superseded export runs")

    resource_id = pick(df, "ResourceId").str.lower()
    resource_group = pick(df, "x_ResourceGroupName", "ResourceGroupName", "ResourceGroup").str.lower()
    resource_group = resource_group.where(
        resource_group.notna(), resource_id.map(lambda r: (RG_RE.search(r) or [None, None])[1] if isinstance(r, str) else None)
    )
    tags = pick(df, "Tags").map(parse_tags)

    out = pd.DataFrame(
        {
            "charge_date": pd.to_datetime(pick(df, "ChargePeriodStart"), utc=True).dt.date,
            "billing_period_start": pd.to_datetime(pick(df, "BillingPeriodStart"), utc=True).dt.date,
            "billing_account_name": pick(df, "BillingAccountName"),
            "subscription_id": pick(df, "SubAccountId").str.lower().str.replace("/subscriptions/", "", regex=False),
            "subscription_name": pick(df, "SubAccountName"),
            "resource_group": resource_group,
            "resource_id": resource_id,
            "resource_name": pick(df, "ResourceName"),
            "resource_type": pick(df, "x_ResourceType", "ResourceType"),
            "service_name": pick(df, "ServiceName").fillna("Unknown"),
            "service_category": pick(df, "ServiceCategory").fillna("Other"),
            "meter_category": pick(df, "x_SkuMeterCategory"),
            "meter_name": pick(df, "x_SkuMeterName"),
            "region": pick(df, "RegionName", "RegionId"),
            "charge_category": pick(df, "ChargeCategory"),
            "charge_description": pick(df, "ChargeDescription"),
            "pricing_category": pick(df, "PricingCategory"),
            "billing_currency": pick(df, "BillingCurrency"),
            "billed_cost": pd.to_numeric(pick(df, "BilledCost"), errors="coerce"),
            "effective_cost": pd.to_numeric(pick(df, "EffectiveCost", "BilledCost"), errors="coerce"),
            "list_cost": pd.to_numeric(pick(df, "ListCost", "EffectiveCost"), errors="coerce"),
            "consumed_quantity": pd.to_numeric(pick(df, "ConsumedQuantity"), errors="coerce"),
            "consumed_unit": pick(df, "ConsumedUnit"),
            "tags": pick(df, "Tags"),
            "tag_environment": tags.map(lambda t: t.get("environment") or t.get("env")),
            "tag_cost_center": tags.map(lambda t: t.get("costcenter") or t.get("cost-center") or t.get("cost_center")),
            "tag_project": tags.map(lambda t: t.get("project") or t.get("application") or t.get("app")),
            "_export_path": df["_export_path"],
            "_billing_period": df["_billing_period"],
            "_export_run": df["_export_run"],
            "_source_file": df["_source_file"],
        }
    )
    out = out[out["charge_date"].notna() & out["effective_cost"].notna()]
    # No row-level dedup: identical FOCUS line items are legitimate; duplicates come only from old export runs.
    print(f"  silver_costs: {len(out)} rows")
    print("\n  effective cost by subscription (silver):")
    print(
        out.groupby("subscription_name")["effective_cost"].sum().sort_values(ascending=False).round(2)
        .to_string().replace("\n", "\n    ")
    )
    print("\n  effective cost by service (silver):")
    print(
        out.groupby("service_name")["effective_cost"].sum().sort_values(ascending=False).round(2)
        .to_string().replace("\n", "\n    ")
    )
    return out


# ─── Gold ────────────────────────────────────────────────────────────────────

def gold(df: pd.DataFrame) -> dict[str, pd.DataFrame]:
    banner("GOLD    - star schema for Power BI (Direct Lake)")
    df = df.copy()
    df["subscription_id"] = df["subscription_id"].fillna("unknown")
    df["resource_key"] = df["resource_id"].fillna(UNASSIGNED + "/" + df["subscription_id"])

    fact = (
        df.groupby(
            ["charge_date", "subscription_id", "resource_key", "service_name", "meter_category", "charge_category",
             "pricing_category", "billing_currency"],
            dropna=False, as_index=False,
        )
        .agg(
            billed_cost=("billed_cost", "sum"),
            effective_cost=("effective_cost", "sum"),
            list_cost=("list_cost", "sum"),
            consumed_quantity=("consumed_quantity", "sum"),
            line_items=("effective_cost", "size"),
        )
    )
    fact["savings"] = fact["list_cost"] - fact["effective_cost"]
    for c in ("billed_cost", "effective_cost", "list_cost", "savings"):
        fact[c] = fact[c].round(4)

    dim_subscription = (
        df.sort_values("charge_date", ascending=False)
        .drop_duplicates("subscription_id")
        [["subscription_id", "subscription_name", "billing_account_name"]]
        .reset_index(drop=True)
    )
    dim_subscription["subscription_name"] = dim_subscription["subscription_name"].fillna(dim_subscription["subscription_id"])
    dim_subscription["billing_account_name"] = dim_subscription["billing_account_name"].fillna("(not set)")

    dim_resource = (
        df.sort_values("charge_date", ascending=False)
        .drop_duplicates("resource_key")
        [["resource_key", "resource_id", "resource_name", "resource_type", "resource_group",
          "subscription_id", "subscription_name", "region", "tag_environment", "tag_cost_center", "tag_project", "tags"]]
        .reset_index(drop=True)
    )
    dim_resource.loc[dim_resource["resource_key"].str.startswith(UNASSIGNED), ["resource_name", "resource_group"]] = \
        ["(no resource)", "(no resource group)"]
    for c in ("resource_name", "resource_group", "tag_environment", "tag_cost_center", "tag_project"):
        dim_resource[c] = dim_resource[c].fillna("(not set)")

    dim_service = (
        df.groupby("service_name", as_index=False)
        .agg(service_category=("service_category", "first"))
    )

    # Full calendar years keep DAX time intelligence (TOTALYTD, DATEADD) correct.
    start = pd.Timestamp(year=pd.Timestamp(min(df["charge_date"])).year, month=1, day=1)
    end = pd.Timestamp(year=pd.Timestamp(max(df["charge_date"])).year, month=12, day=31)
    dates = pd.date_range(start, end, freq="D")
    dim_date = pd.DataFrame(
        {
            "date": dates.date,
            "year": dates.year,
            "quarter": "Q" + dates.quarter.astype(str),
            "month": dates.month,
            "month_name": dates.strftime("%b"),
            "year_month": dates.strftime("%Y-%m"),
            "year_month_sort": dates.year * 100 + dates.month,
            "day": dates.day,
            "day_of_week": dates.dayofweek + 1,
            "day_name": dates.strftime("%a"),
            "is_weekend": dates.dayofweek >= 5,
        }
    )

    monthly = (
        df.assign(year_month=pd.to_datetime(df["charge_date"]).dt.strftime("%Y-%m"))
        .groupby(["year_month", "subscription_id", "service_name"], as_index=False)
        .agg(effective_cost=("effective_cost", "sum"), list_cost=("list_cost", "sum"))
    )
    monthly["cost_year"] = monthly["year_month"].str[:4].astype(int)
    keys = ["year_month", "subscription_id", "service_name"]
    # Previous month is matched by calendar month (not row order), so gaps yield an empty value.
    previous = monthly[keys + ["effective_cost"]].rename(columns={"effective_cost": "previous_month_cost"})
    previous["year_month"] = (pd.to_datetime(previous["year_month"] + "-01") + pd.DateOffset(months=1)).dt.strftime("%Y-%m")
    monthly = monthly.merge(previous, on=keys, how="left").sort_values(["subscription_id", "service_name", "year_month"])
    monthly["mom_change"] = monthly["effective_cost"] - monthly["previous_month_cost"]
    monthly["mom_change_pct"] = monthly["mom_change"] / monthly["previous_month_cost"]
    monthly["ytd_cost"] = monthly.groupby(["subscription_id", "service_name", "cost_year"])["effective_cost"].cumsum()
    for c in ("effective_cost", "list_cost", "previous_month_cost", "mom_change", "ytd_cost"):
        monthly[c] = monthly[c].round(4)
    monthly["mom_change_pct"] = monthly["mom_change_pct"].round(4)

    tables = {
        "gold_fact_cost_daily": fact,
        "gold_dim_subscription": dim_subscription,
        "gold_dim_resource": dim_resource,
        "gold_dim_service": dim_service,
        "gold_dim_date": dim_date,
        "gold_cost_monthly": monthly.drop(columns="cost_year"),
    }
    for name, t in tables.items():
        print(f"  {name:<22} {len(t):>6} rows   columns: {', '.join(t.columns)[:90]}...")

    # Reconciliation: Gold must equal Silver to the cent.
    diff = abs(fact["effective_cost"].sum() - df["effective_cost"].sum())
    print(f"\n  reconciliation silver vs gold effective cost: diff = {diff:.4f} {'OK' if diff < 0.01 else 'MISMATCH'}")
    return tables


def main() -> None:
    here = Path(__file__).parent
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--input", default=str(here / "sample-data"))
    parser.add_argument("--output", default=str(here / "output"))
    args = parser.parse_args()

    out = Path(args.output)
    for layer in ("bronze", "silver", "gold"):
        (out / layer).mkdir(parents=True, exist_ok=True)

    df_bronze = bronze(Path(args.input))
    df_bronze.to_parquet(out / "bronze" / "bronze_costs.parquet", index=False)

    df_silver = silver(df_bronze)
    df_silver.to_parquet(out / "silver" / "silver_costs.parquet", index=False)

    for name, table in gold(df_silver).items():
        table.to_parquet(out / "gold" / f"{name}.parquet", index=False)

    print(f"\nDone. Tables written under {out.resolve()}")


if __name__ == "__main__":
    main()
