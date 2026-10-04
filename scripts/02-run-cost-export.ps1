<#
.SYNOPSIS
  Step 2 - Run every FOCUS cost export now (and optionally backfill previous months), then list the files.

.DESCRIPTION
  The scheduled exports only start tomorrow and only cover month-to-date. For a demo you usually want
  data immediately and some history, so this script calls the Exports "run" API for each export in
  .azure-outputs.json (one per subscription, or one billing-scope export):
    POST {scope}/providers/Microsoft.CostManagement/exports/{name}/run?api-version=2025-03-01
  with an optional { timePeriod: { from, to } } body for each month to backfill.

.EXAMPLE
  ./scripts/02-run-cost-export.ps1 -BackfillMonths 3
#>
[CmdletBinding()]
param(
    [int] $BackfillMonths = 0,
    [int] $WaitMinutes = 30
)
$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path $PSScriptRoot -Parent
$outputsFile = Join-Path $repoRoot '.azure-outputs.json'
if (-not (Test-Path $outputsFile)) { throw 'Run ./scripts/01-deploy-azure.ps1 first.' }
$o = Get-Content $outputsFile -Raw | ConvertFrom-Json
$exportIds = @($o.COST_EXPORT_IDS) + @($o.COST_EXPORT_ID) | Where-Object { $_ } | Select-Object -Unique
if (-not $exportIds) { throw 'No export IDs found in .azure-outputs.json.' }

$api = '2025-03-01'

function Get-ScopeLabel([string] $exportId) {
    if ($exportId -match '^/subscriptions/([^/]+)') { return "subscription $($Matches[1])" }
    return ($exportId -split '/providers/Microsoft.CostManagement')[0]
}

function Invoke-ExportRun([string] $exportId, $from, $to) {
    $label = if ($from) { "$($from.ToString('yyyy-MM-dd')) -> $($to.ToString('yyyy-MM-dd'))" } else { 'month to date' }
    Write-Host ("  {0,-55} {1}" -f (Get-ScopeLabel $exportId), $label)
    $azArgs = @('rest', '--method', 'post', '--url', "https://management.azure.com$exportId/run?api-version=$api")
    $bodyFile = $null
    if ($from) {
        $bodyFile = New-TemporaryFile
        @{ timePeriod = @{ from = $from.ToString('yyyy-MM-ddT00:00:00Z'); to = $to.ToString('yyyy-MM-ddT23:59:59Z') } } |
            ConvertTo-Json | Set-Content $bodyFile -Encoding utf8
        $azArgs += @('--body', "@$bodyFile")
    }
    try {
        az @azArgs | Out-Null
        if ($LASTEXITCODE -ne 0) { Write-Warning "Export run failed: $(Get-ScopeLabel $exportId) $label"; return $false }
        return $true
    } finally {
        if ($bodyFile) { Remove-Item $bodyFile -ErrorAction SilentlyContinue }
    }
}

Write-Host "`n=== Trigger $($exportIds.Count) export(s) '$($o.COST_EXPORT_NAME)' ===" -ForegroundColor Cyan
$started = (Get-Date).ToUniversalTime()
$expected = @{}
$firstOfMonth = Get-Date -Day 1 -Hour 0 -Minute 0 -Second 0
foreach ($id in $exportIds) {
    $expected[$id] = 0
    if (Invoke-ExportRun $id $null $null) { $expected[$id]++ }
    for ($i = 1; $i -le $BackfillMonths; $i++) {
        $from = $firstOfMonth.AddMonths(-$i)
        Start-Sleep -Seconds 5   # stay below the Cost Management throttling limits
        if (Invoke-ExportRun $id $from $from.AddMonths(1).AddDays(-1)) { $expected[$id]++ }
    }
}

Write-Host "`n=== Wait for the runs to finish (usually 2-10 minutes per export) ===" -ForegroundColor Cyan
$deadline = (Get-Date).AddMinutes($WaitMinutes)
$failed = @()
do {
    Start-Sleep -Seconds 30
    $pending = 0
    $failed = @()
    $line = foreach ($id in $exportIds) {
        $history = az rest --method get --url "https://management.azure.com$id`?api-version=$api&`$expand=runHistory" `
            --query 'properties.runHistory.value[].properties' -o json | ConvertFrom-Json
        $recent = @($history | Where-Object { [datetime]$_.submittedTime -ge $started.AddMinutes(-1) })
        $done = @($recent | Where-Object { $_.status -in 'Completed', 'Failed' }).Count
        $failed += @($recent | Where-Object status -eq 'Failed')
        if ($done -lt $expected[$id]) { $pending++ }
        "$done/$($expected[$id])"
    }
    Write-Host ("  {0:HH:mm:ss}  completed per export: {1}" -f (Get-Date), ($line -join '  '))
} while ($pending -gt 0 -and (Get-Date) -lt $deadline)

$failed | ForEach-Object { Write-Warning "Run failed: $($_.error.message)" }
if ($pending -gt 0) { Write-Warning "$pending export(s) still running; check Cost Management > Exports > Run history." }

Write-Host "`n=== Files in container 'costs' ===" -ForegroundColor Cyan
az storage fs file list --account-name $o.AZURE_STORAGE_ACCOUNT_NAME --file-system costs --auth-mode login `
    --query "[?ends_with(name, '.parquet')].{file:name, sizeKB:to_string(floor(contentLength/``1024``))}" -o table
if ($LASTEXITCODE -ne 0) {
    Write-Warning 'Listing failed. New role assignments can take ~5 minutes to apply; retry, or check the container in the portal.'
}
Write-Host "`nNext: python ./scripts/setup_fabric.py --capacity-name <your-capacity>" -ForegroundColor Green
