"""Generate synthetic Azure Cost Management FOCUS 1.0 exports for the offline demo.

The output mirrors the folder layout of one Cost Management export per subscription,
all writing to the same ADLS Gen2 container, so the same Bronze logic can read it:

    <out>/costs/focus/<subscription-id>/<export-name>/<yyyyMMdd-yyyyMMdd>/<run-id>/part_0_0001.parquet
                                                                                 /manifest.json

Three subscriptions are generated. The current month is written twice (an older and
a newer run) per subscription to demonstrate why Silver keeps only the latest run
per export folder and billing period.

Usage:
    python generate_sample_focus.py --out ./sample-data --months 3
"""

from __future__ import annotations

import argparse
import json
import os
import random
import time
import uuid
import zlib
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

EXPORT_NAME = "focus-daily-demo"
SUBSCRIPTIONS = {
    "prod": ("11111111-2222-3333-4444-555555555555", "contoso-prod"),
    "platform": ("22222222-3333-4444-5555-666666666666", "contoso-platform"),
    "dev": ("33333333-4444-5555-6666-777777777777", "contoso-dev"),
}
RG_SUBSCRIPTION = {
    "rg-agent-prod": "prod", "rg-ai-prod": "prod", "rg-web-prod": "prod",
    "rg-data-prod": "platform", "rg-shared": "platform",
    "rg-dev": "dev",
}
BILLING_ACCOUNT_ID = "/providers/Microsoft.Billing/billingAccounts/demo-ba"
BILLING_ACCOUNT_NAME = "Contoso Ltd."
CURRENCY = "USD"

# (resource group, resource name, provider type, ServiceName, ServiceCategory,
#  x_SkuMeterCategory, meter name, region, avg daily list cost, pricing category, tags)
RESOURCES = [
    ("rg-agent-prod", "vm-agent-01", "Microsoft.Compute/virtualMachines", "Virtual Machines", "Compute",
     "Virtual Machines", "D4s v5", "westeurope", 9.20, "Committed",
     {"environment": "prod", "costCenter": "CC-1001", "project": "agentops"}),
    ("rg-agent-prod", "vm-agent-02", "Microsoft.Compute/virtualMachines", "Virtual Machines", "Compute",
     "Virtual Machines", "D4s v5", "westeurope", 9.20, "Committed",
     {"environment": "prod", "costCenter": "CC-1001", "project": "agentops"}),
    ("rg-agent-prod", "aks-agent", "Microsoft.ContainerService/managedClusters", "Azure Kubernetes Service", "Compute",
     "Azure Kubernetes Service", "Standard Uptime SLA", "westeurope", 2.40, "Standard",
     {"environment": "prod", "costCenter": "CC-1001", "project": "agentops"}),
    ("rg-agent-prod", "cosmos-agent", "Microsoft.DocumentDB/databaseAccounts", "Azure Cosmos DB", "Databases",
     "Azure Cosmos DB", "RU/s", "westeurope", 6.80, "Standard",
     {"environment": "prod", "costCenter": "CC-1001", "project": "agentops"}),
    ("rg-ai-prod", "aoai-agent", "Microsoft.CognitiveServices/accounts", "Azure OpenAI Service", "AI and Machine Learning",
     "Foundry Models", "gpt-4o Input Tokens", "swedencentral", 14.50, "Standard",
     {"environment": "prod", "costCenter": "CC-2002", "project": "agentops"}),
    ("rg-web-prod", "app-frontend", "Microsoft.Web/sites", "Azure App Service", "Web",
     "Azure App Service", "P1v3 App", "westeurope", 4.10, "Standard",
     {"environment": "prod", "costCenter": "CC-3003", "project": "portal"}),
    ("rg-data-prod", "stdatalake01", "Microsoft.Storage/storageAccounts", "Storage Accounts", "Storage",
     "Storage", "Hot LRS Data Stored", "westeurope", 1.90, "Standard",
     {"environment": "prod", "costCenter": "CC-4004", "project": "dataplatform"}),
    ("rg-data-prod", "sql-reporting", "Microsoft.Sql/servers/databases", "Azure SQL Database", "Databases",
     "SQL Database", "vCore", "westeurope", 5.30, "Standard",
     {"Environment": "prod", "CostCenter": "CC-4004", "Project": "dataplatform"}),
    ("rg-shared", "log-central", "Microsoft.OperationalInsights/workspaces", "Log Analytics", "Management and Governance",
     "Log Analytics", "Analytics Logs Data Ingestion", "westeurope", 3.60, "Standard",
     {"environment": "shared", "costCenter": "CC-9000"}),
    ("rg-shared", "vnet-hub", "Microsoft.Network/virtualNetworks", "Virtual Network", "Networking",
     "Bandwidth", "Inter-Region Data Transfer Out", "westeurope", 0.80, "Standard",
     {"environment": "shared"}),
    ("rg-dev", "vm-dev-01", "Microsoft.Compute/virtualMachines", "Virtual Machines", "Compute",
     "Virtual Machines", "B2ms", "northeurope", 1.70, "Standard",
     {"environment": "dev", "costCenter": "CC-1001", "project": "agentops"}),
    ("rg-dev", "stdevscratch", "Microsoft.Storage/storageAccounts", "Storage Accounts", "Storage",
     "Storage", "Hot LRS Data Stored", "northeurope", 0.35, "Standard",
     {}),
]

COMMITMENT_DISCOUNT = 0.38  # reserved instance discount vs. list price
NEGOTIATED_DISCOUNT = 0.05  # enterprise discount applied to everything else


def month_start(d: date) -> date:
    return d.replace(day=1)


def next_month(d: date) -> date:
    return (d.replace(day=28) + timedelta(days=4)).replace(day=1)


def utc(d: date) -> datetime:
    return datetime(d.year, d.month, d.day, tzinfo=timezone.utc)


def build_rows(day: date, rng: random.Random, growth: float) -> list[dict]:
    period_start = month_start(day)
    period_end = next_month(day)
    rows = []
    weekend = day.weekday() >= 5

    for rg, name, rtype, service, category, meter_cat, meter, region, daily, pricing, tags in RESOURCES:
        # Dev resources are shut down on weekends; everything has noise and slight growth.
        if rg == "rg-dev" and weekend and rtype.endswith("virtualMachines"):
            continue
        noise = rng.uniform(0.85, 1.18)
        list_cost = round(daily * noise * growth, 6)
        if service == "Azure OpenAI Service" and day.day in (14, 15, 16):
            list_cost *= 2.6  # a load-test spike worth spotting in the report
        discount = COMMITMENT_DISCOUNT if pricing == "Committed" else NEGOTIATED_DISCOUNT
        effective = round(list_cost * (1 - discount), 6)
        # Committed usage is billed through the reservation purchase, not the usage row.
        billed = 0.0 if pricing == "Committed" else effective
        sub_id, sub_name = SUBSCRIPTIONS[RG_SUBSCRIPTION[rg]]
        resource_id = (
            f"/subscriptions/{sub_id}/resourceGroups/{rg}/providers/{rtype}/{name}"
        )
        rows.append(
            {
                "BilledCost": billed,
                "BillingAccountId": BILLING_ACCOUNT_ID,
                "BillingAccountName": BILLING_ACCOUNT_NAME,
                "BillingCurrency": CURRENCY,
                "BillingPeriodStart": utc(period_start),
                "BillingPeriodEnd": utc(period_end),
                "ChargeCategory": "Usage",
                "ChargeClass": None,
                "ChargeDescription": f"{meter} - {name}",
                "ChargeFrequency": "Usage-Based",
                "ChargePeriodStart": utc(day),
                "ChargePeriodEnd": utc(day + timedelta(days=1)),
                "CommitmentDiscountCategory": "Usage" if pricing == "Committed" else None,
                "ConsumedQuantity": round(list_cost * 3.1, 4),
                "ConsumedUnit": "Hours" if "Machines" in service else "Units",
                "ContractedCost": round(list_cost * (1 - NEGOTIATED_DISCOUNT), 6),
                "EffectiveCost": effective,
                "ListCost": list_cost,
                "PricingCategory": pricing,
                "PricingUnit": "1 Hour" if "Machines" in service else "1 Unit",
                "ProviderName": "Microsoft",
                "PublisherName": "Microsoft",
                "RegionId": region,
                "RegionName": region.replace("europe", " Europe").replace("central", " Central").title(),
                "ResourceId": resource_id,
                "ResourceName": name,
                "ResourceType": rtype.split("/")[-1],
                "ServiceCategory": category,
                "ServiceName": service,
                "SkuId": f"SKU-{zlib.crc32(meter.encode()) % 10_000:04d}",
                "SubAccountId": f"/subscriptions/{sub_id}",
                "SubAccountName": sub_name,
                "SubAccountType": "Subscription",
                "Tags": json.dumps(tags) if tags else None,
                "x_ResourceGroupName": rg,
                "x_ResourceType": rtype,
                "x_SkuMeterCategory": meter_cat,
                "x_SkuMeterName": meter,
            }
        )

    if day.day == 1:
        # Monthly reservation purchase (prod) and a support plan (platform): no resource attached.
        for desc, svc, cat, cost, sub_key in (
            ("Reserved VM Instance, D4s v5, 1 Year", "Virtual Machines", "Compute", 210.0, "prod"),
            ("Azure Support - Standard", "Azure Support", "Other", 100.0, "platform"),
        ):
            sub_id, sub_name = SUBSCRIPTIONS[sub_key]
            rows.append(
                {
                    "BilledCost": cost, "BillingAccountId": BILLING_ACCOUNT_ID,
                    "BillingAccountName": BILLING_ACCOUNT_NAME, "BillingCurrency": CURRENCY,
                    "BillingPeriodStart": utc(period_start), "BillingPeriodEnd": utc(period_end),
                    "ChargeCategory": "Purchase", "ChargeClass": None, "ChargeDescription": desc,
                    "ChargeFrequency": "Recurring", "ChargePeriodStart": utc(day),
                    "ChargePeriodEnd": utc(period_end), "CommitmentDiscountCategory": None,
                    "ConsumedQuantity": None, "ConsumedUnit": None, "ContractedCost": cost,
                    # Amortized reservation cost is already spread onto usage rows.
                    "EffectiveCost": 0.0 if "Reserved" in desc else cost,
                    "ListCost": cost, "PricingCategory": "Standard", "PricingUnit": "1 Month",
                    "ProviderName": "Microsoft", "PublisherName": "Microsoft", "RegionId": None,
                    "RegionName": None, "ResourceId": None, "ResourceName": None, "ResourceType": None,
                    "ServiceCategory": cat, "ServiceName": svc, "SkuId": None,
                    "SubAccountId": f"/subscriptions/{sub_id}",
                    "SubAccountName": sub_name, "SubAccountType": "Subscription", "Tags": None,
                    "x_ResourceGroupName": None, "x_ResourceType": None,
                    "x_SkuMeterCategory": None, "x_SkuMeterName": None,
                }
            )
    return rows


def write_run(root: Path, subscription_id: str, period_start: date, rows: list[dict], mtime: float) -> Path:
    period_end = next_month(period_start) - timedelta(days=1)
    period_folder = f"{period_start:%Y%m%d}-{period_end:%Y%m%d}"
    run_id = str(uuid.uuid4())
    run_dir = root / "costs" / "focus" / subscription_id / EXPORT_NAME / period_folder / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    part = run_dir / "part_0_0001.parquet"
    pd.DataFrame(rows).to_parquet(part, index=False)
    manifest = {
        "exportConfig": {"exportName": EXPORT_NAME, "type": "FocusCost", "dataVersion": "1.0"},
        "runInfo": {"runId": run_id, "submittedTime": datetime.fromtimestamp(mtime, timezone.utc).isoformat()},
        "blobs": [{"blobName": part.name, "dataRowCount": len(rows)}],
    }
    (run_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    for f in (part, run_dir / "manifest.json"):
        os.utime(f, (mtime, mtime))
    return part


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", default=str(Path(__file__).parent / "sample-data"))
    parser.add_argument("--months", type=int, default=3, help="Full months of history before the current month.")
    parser.add_argument("--end-date", default=None, help="Last day with cost data (default: yesterday).")
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    rng = random.Random(args.seed)
    end = date.fromisoformat(args.end_date) if args.end_date else date.today() - timedelta(days=1)
    first = month_start(end)
    for _ in range(args.months):
        first = month_start(first - timedelta(days=1))

    root = Path(args.out)
    now = time.time()
    period = first
    total_rows = 0
    while period <= end:
        last_day = min(next_month(period) - timedelta(days=1), end)
        rows, day = [], period
        while day <= last_day:
            growth = 1 + 0.004 * ((day - first).days)
            rows.extend(build_rows(day, rng, growth))
            day += timedelta(days=1)

        is_current = period == month_start(end)
        by_subscription: dict[str, list[dict]] = defaultdict(list)
        for r in rows:
            by_subscription[r["SubAccountId"].split("/")[-1]].append(r)
        for sub_id, sub_rows in by_subscription.items():
            if is_current:
                # An older, partial run of the current month that Silver must ignore.
                older = [r for r in sub_rows if r["ChargePeriodStart"].date() < last_day] or sub_rows[: max(1, len(sub_rows) // 2)]
                p = write_run(root, sub_id, period, older, now - 86_400)
                print(f"  older run  {p.relative_to(root)}  ({len(older)} rows)")
            p = write_run(root, sub_id, period, sub_rows, now - (0 if is_current else 3600))
            print(f"  latest run {p.relative_to(root)}  ({len(sub_rows)} rows)")
        total_rows += len(rows)
        period = next_month(period)

    print(f"\nSample FOCUS exports for {len(SUBSCRIPTIONS)} subscriptions written to {root.resolve()} ({total_rows} current rows).")


if __name__ == "__main__":
    main()
