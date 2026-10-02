"""Provision the Fabric side of the Azure Cost -> Fabric solution via REST APIs.

Steps (each one is idempotent, so the script can be re-run safely):
  1. Workspace (created or reused) on a Fabric capacity
  2. Workspace identity + "Storage Blob Data Reader" on the cost storage account
  3. ADLS Gen2 cloud connection that authenticates with the workspace identity
  4. Lakehouse "CostLakehouse" (schema enabled) + OneLake shortcut Files/costs -> container costs
  5. Bronze / Silver / Gold notebooks (bound to the Lakehouse) + "Cost Refresh" pipeline
  6. Run the pipeline once and wait
  7. Direct Lake semantic model "Azure Cost Model" on the Gold tables
  8. Optional daily pipeline schedule

Usage:
    python scripts/setup_fabric.py --capacity-name <capacity> [--workspace-name "Azure Cost Analytics"]
    python scripts/setup_fabric.py --capacity-id <guid> --storage-account <name> --connection-id <guid>
"""

from __future__ import annotations

import base64
import json
import pathlib
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import click
import requests
from azure.identity import AzureCliCredential, ChainedTokenCredential, DefaultAzureCredential
from rich.console import Console
from rich.table import Table

FABRIC_API = "https://api.fabric.microsoft.com/v1"
POWERBI_API = "https://api.powerbi.com/v1.0/myorg"
ARM_API = "https://management.azure.com"
SCOPES = {
    "fabric": "https://api.fabric.microsoft.com/.default",
    "powerbi": "https://analysis.windows.net/powerbi/api/.default",
    "arm": "https://management.azure.com/.default",
}
STORAGE_BLOB_DATA_READER = "2a2b9908-6ea1-4ae2-8e65-a410df84e7d1"

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
NOTEBOOKS_DIR = REPO_ROOT / "fabric" / "notebooks"
PIPELINE_FILE = REPO_ROOT / "fabric" / "pipelines" / "pipeline_cost_refresh.json"
MODEL_FILE = REPO_ROOT / "fabric" / "semantic-model" / "model.bim.json"
AZURE_OUTPUTS = REPO_ROOT / ".azure-outputs.json"
FABRIC_OUTPUTS = REPO_ROOT / ".fabric-outputs.json"

LAKEHOUSE_NAME = "CostLakehouse"
SEMANTIC_MODEL_NAME = "Azure Cost Model"
GOLD_TABLES = ["gold_fact_cost_daily", "gold_dim_date", "gold_dim_resource", "gold_dim_service", "gold_cost_monthly"]

console = Console()


class Api:
    """Minimal REST client with token caching and long-running-operation support."""

    def __init__(self) -> None:
        self._cred = ChainedTokenCredential(AzureCliCredential(), DefaultAzureCredential(exclude_cli_credential=True))
        self._tokens: dict[str, Any] = {}

    def _token(self, audience: str) -> str:
        tok = self._tokens.get(audience)
        if not tok or tok.expires_on - time.time() < 300:
            tok = self._cred.get_token(SCOPES[audience])
            self._tokens[audience] = tok
        return tok.token

    def request(self, method: str, url: str, audience: str = "fabric", ok=(200, 201, 202, 204), **kw) -> requests.Response:
        base = {"fabric": FABRIC_API, "powerbi": POWERBI_API, "arm": ARM_API}[audience]
        full = url if url.startswith("http") else f"{base}{url}"
        for attempt in range(6):
            resp = requests.request(
                method, full, headers={"Authorization": f"Bearer {self._token(audience)}"}, timeout=120, **kw
            )
            if resp.status_code == 429:
                wait = int(resp.headers.get("Retry-After", "20"))
                console.print(f"    [dim]throttled, retrying in {wait}s[/dim]")
                time.sleep(wait)
                continue
            break
        if resp.status_code not in ok:
            raise ApiError(resp)
        return resp

    def get(self, url: str, audience: str = "fabric") -> dict[str, Any]:
        resp = self.request("GET", url, audience)
        return resp.json() if resp.content else {}

    def list(self, url: str) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        while url:
            body = self.get(url)
            items.extend(body.get("value", []))
            url = body.get("continuationUri") or ""
        return items

    def wait_lro(self, resp: requests.Response, timeout: int = 1200) -> dict[str, Any]:
        """Follow a Fabric 202 Accepted long-running operation to completion."""
        if resp.status_code != 202:
            return resp.json() if resp.content else {}
        op_id = resp.headers.get("x-ms-operation-id")
        location = resp.headers.get("Location")
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            time.sleep(int(resp.headers.get("Retry-After", "5")))
            status = self.get(f"/operations/{op_id}") if op_id else self.get(location)
            state = status.get("status")
            if state == "Succeeded":
                try:
                    return self.get(f"/operations/{op_id}/result")
                except ApiError:
                    return status
            if state == "Failed":
                raise RuntimeError(f"Fabric operation failed: {json.dumps(status.get('error'), indent=2)}")
        raise TimeoutError("Fabric operation timed out")


class ApiError(RuntimeError):
    def __init__(self, resp: requests.Response) -> None:
        self.status = resp.status_code
        self.body = resp.text
        try:
            self.code = resp.json().get("errorCode") or resp.json().get("error", {}).get("code")
        except ValueError:
            self.code = None
        super().__init__(f"{resp.request.method} {resp.url} -> {resp.status_code}: {resp.text[:800]}")


def step(n: int, title: str) -> None:
    console.rule(f"[bold cyan]Step {n}: {title}")


def b64(data: bytes | str) -> str:
    return base64.b64encode(data.encode("utf-8") if isinstance(data, str) else data).decode("ascii")


# ─── Fabric building blocks ──────────────────────────────────────────────────

def resolve_capacity(api: Api, capacity_id: str | None, capacity_name: str | None) -> dict[str, Any]:
    capacities = api.list("/capacities")
    for c in capacities:
        if c["id"] == capacity_id or (capacity_name and c["displayName"].lower() == capacity_name.lower()):
            if c.get("state") != "Active":
                raise click.ClickException(
                    f"Capacity '{c['displayName']}' is {c.get('state')}. Resume it in the Azure portal first."
                )
            return c
    names = ", ".join(f"{c['displayName']} ({c.get('sku')}, {c.get('state')})" for c in capacities)
    raise click.ClickException(f"Capacity not found. Available: {names}")


def ensure_workspace(api: Api, name: str, capacity_id: str) -> dict[str, Any]:
    for ws in api.list("/workspaces"):
        if ws["displayName"] == name:
            console.print(f"  [yellow]reusing workspace '{name}'[/yellow]")
            if ws.get("capacityId", "").lower() != capacity_id.lower():
                api.wait_lro(api.request("POST", f"/workspaces/{ws['id']}/assignToCapacity", json={"capacityId": capacity_id}))
                console.print("  [green]assigned to capacity[/green]")
            return ws
    ws = api.request("POST", "/workspaces", json={"displayName": name, "capacityId": capacity_id}).json()
    console.print(f"  [green]created workspace '{name}'[/green]")
    return ws


def ensure_workspace_identity(api: Api, workspace_id: str) -> str:
    ws = api.get(f"/workspaces/{workspace_id}")
    identity = ws.get("workspaceIdentity")
    if not identity:
        console.print("  provisioning workspace identity (takes ~1 min)...")
        api.wait_lro(api.request("POST", f"/workspaces/{workspace_id}/provisionIdentity"))
        identity = api.get(f"/workspaces/{workspace_id}").get("workspaceIdentity")
    if not identity:
        raise RuntimeError("Workspace identity could not be provisioned.")
    console.print(f"  workspace identity service principal: [bold]{identity['servicePrincipalId']}[/bold]")
    return identity["servicePrincipalId"]


def grant_storage_reader(api: Api, storage_id: str, principal_id: str) -> None:
    assignment = uuid.uuid5(uuid.NAMESPACE_URL, f"{storage_id}/{principal_id}/{STORAGE_BLOB_DATA_READER}")
    sub = storage_id.split("/")[2]
    body = {
        "properties": {
            "roleDefinitionId": f"/subscriptions/{sub}/providers/Microsoft.Authorization/roleDefinitions/{STORAGE_BLOB_DATA_READER}",
            "principalId": principal_id,
            "principalType": "ServicePrincipal",
        }
    }
    url = f"{storage_id}/providers/Microsoft.Authorization/roleAssignments/{assignment}?api-version=2022-04-01"
    try:
        api.request("PUT", url, audience="arm", json=body)
        console.print("  [green]granted Storage Blob Data Reader to the workspace identity[/green]")
    except ApiError as exc:
        if exc.status == 409:
            console.print("  [yellow]role assignment already exists[/yellow]")
        else:
            raise


def ensure_connection(api: Api, storage_account: str, dfs_url: str) -> str:
    name = f"adls-{storage_account}-costs-wi"
    for conn in api.list("/connections"):
        if conn.get("displayName") == name:
            console.print(f"  [yellow]reusing connection '{name}'[/yellow]")
            return conn["id"]

    body = {
        "connectivityType": "ShareableCloud",
        "displayName": name,
        "privacyLevel": "Organizational",
        "connectionDetails": {
            "type": "AzureDataLakeStorage",
            "creationMethod": "AzureDataLakeStorage",
            "parameters": [
                {"dataType": "Text", "name": "server", "value": dfs_url.rstrip("/")},
                {"dataType": "Text", "name": "path", "value": "costs"},
            ],
        },
        "credentialDetails": {
            "singleSignOnType": "None",
            "connectionEncryption": "NotEncrypted",
            "skipTestConnection": False,
            "credentials": {"credentialType": "WorkspaceIdentity"},
        },
    }
    # Fabric tests the connection on creation; new RBAC assignments can take a few minutes to propagate.
    for attempt in range(1, 16):
        try:
            conn = api.request("POST", "/connections", json=body).json()
            console.print(f"  [green]created connection '{name}'[/green]")
            return conn["id"]
        except ApiError as exc:
            if exc.status in (400, 401, 403) and attempt < 15:
                console.print(f"    [dim]connection test failed ({exc.code or exc.status}); waiting for RBAC propagation... ({attempt}/15)[/dim]")
                time.sleep(30)
                continue
            raise
    raise RuntimeError("unreachable")


def find_item(api: Api, workspace_id: str, name: str, item_type: str) -> dict[str, Any] | None:
    for item in api.list(f"/workspaces/{workspace_id}/items?type={item_type}"):
        if item["displayName"] == name:
            return item
    return None


def upsert_item(api: Api, workspace_id: str, name: str, item_type: str, definition: dict | None = None,
                creation_payload: dict | None = None) -> dict[str, Any]:
    existing = find_item(api, workspace_id, name, item_type)
    if existing:
        if definition:
            resp = api.request("POST", f"/workspaces/{workspace_id}/items/{existing['id']}/updateDefinition",
                               json={"definition": definition})
            api.wait_lro(resp)
            console.print(f"  [yellow]updated {item_type} '{name}'[/yellow]")
        else:
            console.print(f"  [yellow]reusing {item_type} '{name}'[/yellow]")
        return existing
    payload: dict[str, Any] = {"displayName": name, "type": item_type}
    if definition:
        payload["definition"] = definition
    if creation_payload:
        payload["creationPayload"] = creation_payload
    item = api.wait_lro(api.request("POST", f"/workspaces/{workspace_id}/items", json=payload))
    if not item.get("id"):
        item = find_item(api, workspace_id, name, item_type) or item
    console.print(f"  [green]created {item_type} '{name}'[/green]")
    return item


def ensure_shortcut(api: Api, workspace_id: str, lakehouse_id: str, dfs_url: str, connection_id: str) -> None:
    base = f"/workspaces/{workspace_id}/items/{lakehouse_id}/shortcuts"
    try:
        api.get(f"{base}/Files/costs")
        console.print("  [yellow]shortcut Files/costs already exists[/yellow]")
        return
    except ApiError as exc:
        if exc.status != 404:
            raise
    body = {
        "path": "Files",
        "name": "costs",
        "target": {"adlsGen2": {"location": dfs_url.rstrip("/"), "subpath": "/costs", "connectionId": connection_id}},
    }
    for attempt in range(1, 11):
        try:
            api.request("POST", base, json=body)
            console.print("  [green]created shortcut Files/costs -> abfss://costs[/green]")
            return
        except ApiError as exc:
            if exc.status in (400, 403) and attempt < 10:
                console.print(f"    [dim]shortcut not ready ({exc.code or exc.status}), retrying in 30s ({attempt}/10)[/dim]")
                time.sleep(30)
                continue
            raise


def import_notebooks(api: Api, workspace_id: str, lakehouse: dict[str, Any]) -> list[dict[str, Any]]:
    results = []
    for nb_path in sorted(NOTEBOOKS_DIR.glob("*.ipynb")):
        nb = json.loads(nb_path.read_text(encoding="utf-8"))
        nb.setdefault("metadata", {}).setdefault("dependencies", {})["lakehouse"] = {
            "default_lakehouse": lakehouse["id"],
            "default_lakehouse_name": lakehouse["displayName"],
            "default_lakehouse_workspace_id": workspace_id,
        }
        definition = {
            "format": "ipynb",
            "parts": [{"path": "artifact.content.ipynb", "payload": b64(json.dumps(nb)), "payloadType": "InlineBase64"}],
        }
        results.append(upsert_item(api, workspace_id, nb_path.stem, "Notebook", definition=definition))
    return results


def import_pipeline(api: Api, workspace_id: str, notebooks: list[dict[str, Any]]) -> dict[str, Any]:
    content = json.loads(PIPELINE_FILE.read_text(encoding="utf-8"))
    by_name = {nb["displayName"]: nb["id"] for nb in notebooks}
    for activity in content["properties"]["activities"]:
        if activity["type"] == "TridentNotebook":
            props = activity["typeProperties"]
            props["notebookId"] = by_name[props["notebookId"]]
            props["workspaceId"] = workspace_id
    definition = {"parts": [{"path": "pipeline-content.json", "payload": b64(json.dumps(content)), "payloadType": "InlineBase64"}]}
    return upsert_item(api, workspace_id, content["name"], "DataPipeline", definition=definition)


def run_pipeline(api: Api, workspace_id: str, pipeline_id: str, timeout: int = 3600) -> None:
    resp = api.request("POST", f"/workspaces/{workspace_id}/items/{pipeline_id}/jobs/instances?jobType=Pipeline")
    location = resp.headers["Location"]
    console.print("  pipeline started; Bronze -> Silver -> Gold usually takes 5-10 minutes (Spark start-up included)")
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        time.sleep(20)
        job = api.get(location)
        status = job.get("status")
        console.print(f"    [dim]{int(time.monotonic() - start):>4}s  status={status}[/dim]")
        if status == "Completed":
            console.print("  [green]pipeline completed[/green]")
            return
        if status in ("Failed", "Cancelled", "Deduped"):
            raise RuntimeError(f"Pipeline run {status}: {json.dumps(job.get('failureReason'), indent=2)}")
    raise TimeoutError("Pipeline did not finish in time")


def refresh_sql_endpoint(api: Api, workspace_id: str, sql_endpoint_id: str) -> None:
    try:
        resp = api.request("POST", f"/workspaces/{workspace_id}/sqlEndpoints/{sql_endpoint_id}/refreshMetadata", json={})
        api.wait_lro(resp)
        console.print("  [green]SQL analytics endpoint metadata refreshed[/green]")
    except ApiError as exc:
        console.print(f"  [yellow]metadata refresh skipped ({exc.status}); continuing[/yellow]")


def deploy_semantic_model(api: Api, workspace_id: str, lakehouse_id: str) -> dict[str, Any]:
    lh = api.get(f"/workspaces/{workspace_id}/lakehouses/{lakehouse_id}")
    sql = lh["properties"]["sqlEndpointProperties"]
    refresh_sql_endpoint(api, workspace_id, sql["id"])

    model = MODEL_FILE.read_text(encoding="utf-8")
    model = model.replace("{{SQL_ENDPOINT}}", sql["connectionString"]).replace("{{SQL_ENDPOINT_ID}}", sql["id"])
    pbism = json.dumps({"version": "1.0", "settings": {}})
    definition = {
        "parts": [
            {"path": "model.bim", "payload": b64(model), "payloadType": "InlineBase64"},
            {"path": "definition.pbism", "payload": b64(pbism), "payloadType": "InlineBase64"},
        ],
    }
    item = upsert_item(api, workspace_id, SEMANTIC_MODEL_NAME, "SemanticModel", definition=definition)

    # Frame the Direct Lake model so the first report opens instantly.
    api.request("POST", f"/groups/{workspace_id}/datasets/{item['id']}/refreshes", audience="powerbi",
                json={"type": "full"})
    for _ in range(30):
        time.sleep(10)
        runs = api.get(f"/groups/{workspace_id}/datasets/{item['id']}/refreshes?$top=1", audience="powerbi")["value"]
        status = runs[0].get("status") if runs else "Unknown"
        if status == "Completed":
            console.print("  [green]semantic model refreshed (Direct Lake framing done)[/green]")
            break
        if status == "Failed":
            raise RuntimeError(f"Semantic model refresh failed: {runs[0].get('serviceExceptionJson')}")
    return item


def schedule_pipeline(api: Api, workspace_id: str, pipeline_id: str, at: str) -> None:
    url = f"/workspaces/{workspace_id}/items/{pipeline_id}/jobs/Pipeline/schedules"
    for existing in api.list(url):
        if existing.get("configuration", {}).get("times") == [at]:
            console.print(f"  [yellow]daily schedule at {at} UTC already exists[/yellow]")
            return
    now = datetime.now(timezone.utc)
    body = {
        "enabled": True,
        "configuration": {
            "type": "Daily",
            "times": [at],
            "startDateTime": now.strftime("%Y-%m-%dT%H:%M:%S"),
            "endDateTime": (now + timedelta(days=365 * 3)).strftime("%Y-%m-%dT%H:%M:%S"),
            "localTimeZoneId": "UTC",
        },
    }
    api.request("POST", url, json=body)
    console.print(f"  [green]pipeline scheduled daily at {at} UTC[/green]")


def storage_from_outputs(storage_account: str | None) -> tuple[str, str, str | None]:
    outputs = json.loads(AZURE_OUTPUTS.read_text(encoding="utf-8")) if AZURE_OUTPUTS.exists() else {}
    name = storage_account or outputs.get("AZURE_STORAGE_ACCOUNT_NAME")
    if not name:
        raise click.ClickException("Pass --storage-account or run scripts/01-deploy-azure.ps1 first (.azure-outputs.json).")
    dfs = outputs.get("AZURE_STORAGE_DFS_URL") if name == outputs.get("AZURE_STORAGE_ACCOUNT_NAME") else None
    sid = outputs.get("AZURE_STORAGE_ACCOUNT_ID") if name == outputs.get("AZURE_STORAGE_ACCOUNT_NAME") else None
    return name, (dfs or f"https://{name}.dfs.core.windows.net/"), sid


def lookup_storage_id(api: Api, name: str) -> str:
    query = {"query": f"resources | where type =~ 'microsoft.storage/storageaccounts' and name =~ '{name}' | project id"}
    resp = api.request("POST", "/providers/Microsoft.ResourceGraph/resources?api-version=2022-10-01", audience="arm", json=query)
    rows = resp.json().get("data", [])
    if not rows:
        raise click.ClickException(f"Storage account '{name}' not found in your subscriptions.")
    return rows[0]["id"]


# ─── CLI ─────────────────────────────────────────────────────────────────────

@click.command()
@click.option("--workspace-name", default="Azure Cost Analytics", show_default=True)
@click.option("--capacity-id", help="Fabric capacity GUID.")
@click.option("--capacity-name", help="Fabric capacity display name (alternative to --capacity-id).")
@click.option("--storage-account", help="Cost landing storage account (default: from .azure-outputs.json).")
@click.option("--connection-id", help="Existing ADLS Gen2 connection ID. If omitted, a workspace-identity connection is created.")
@click.option("--skip-run", is_flag=True, help="Do not run the pipeline after provisioning.")
@click.option("--skip-semantic-model", is_flag=True, help="Do not deploy the Direct Lake semantic model.")
@click.option("--schedule-time", default=None, help="Daily pipeline run time in UTC, e.g. 06:00.")
def main(workspace_name, capacity_id, capacity_name, storage_account, connection_id, skip_run, skip_semantic_model, schedule_time):
    """Provision the Fabric workspace that turns Azure cost exports into a Power BI-ready model."""
    if not (capacity_id or capacity_name):
        raise click.ClickException("Provide --capacity-id or --capacity-name.")
    api = Api()
    state: dict[str, Any] = {}
    try:
        step(1, "Workspace")
        capacity = resolve_capacity(api, capacity_id, capacity_name)
        ws = ensure_workspace(api, workspace_name, capacity["id"])
        state.update(workspace_id=ws["id"], workspace_name=workspace_name, capacity=capacity["displayName"])

        name, dfs_url, storage_id = storage_from_outputs(storage_account)
        state.update(storage_account=name)

        if not connection_id:
            step(2, "Workspace identity + storage RBAC")
            principal = ensure_workspace_identity(api, ws["id"])
            grant_storage_reader(api, storage_id or lookup_storage_id(api, name), principal)

            step(3, "ADLS Gen2 connection (workspace identity)")
            connection_id = ensure_connection(api, name, dfs_url)
        else:
            console.print("  using the provided connection; skipping steps 2-3")
        state.update(connection_id=connection_id)

        step(4, "Lakehouse + OneLake shortcut")
        lakehouse = upsert_item(api, ws["id"], LAKEHOUSE_NAME, "Lakehouse", creation_payload={"enableSchemas": True})
        lakehouse = api.get(f"/workspaces/{ws['id']}/lakehouses/{lakehouse['id']}")
        ensure_shortcut(api, ws["id"], lakehouse["id"], dfs_url, connection_id)
        state.update(lakehouse_id=lakehouse["id"])

        step(5, "Notebooks + pipeline")
        notebooks = import_notebooks(api, ws["id"], lakehouse)
        pipeline = import_pipeline(api, ws["id"], notebooks)
        state.update(pipeline_id=pipeline["id"], notebooks={n["displayName"]: n["id"] for n in notebooks})

        if not skip_run:
            step(6, "Run the Bronze -> Silver -> Gold pipeline")
            run_pipeline(api, ws["id"], pipeline["id"])

        if not skip_semantic_model:
            step(7, "Direct Lake semantic model")
            if skip_run:
                console.print("  [yellow]note: Gold tables must exist; run the pipeline before deploying the model[/yellow]")
            model = deploy_semantic_model(api, ws["id"], lakehouse["id"])
            state.update(semantic_model_id=model["id"])

        if schedule_time:
            step(8, "Daily schedule")
            schedule_pipeline(api, ws["id"], pipeline["id"], schedule_time)

    except ApiError as exc:
        console.print(f"\n[bold red]Fabric/Azure API error[/bold red] {exc}")
        sys.exit(1)
    finally:
        if state:
            FABRIC_OUTPUTS.write_text(json.dumps(state, indent=2), encoding="utf-8")

    table = Table(title="Provisioned", show_lines=True)
    table.add_column("Item")
    table.add_column("Value")
    for k, v in state.items():
        table.add_row(k, json.dumps(v) if isinstance(v, dict) else str(v))
    console.print(table)
    console.print(f"\nOpen the workspace: https://app.fabric.microsoft.com/groups/{state['workspace_id']}")


if __name__ == "__main__":
    main()
