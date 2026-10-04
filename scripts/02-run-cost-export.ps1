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
  ./scripts/02-run-cost-export.ps1 -WaitOnly -SinceMinutes 30   # only wait for runs triggered in the last 30 minutes
#>
[CmdletBinding()]
param(
    [int] $BackfillMonths = 0,
    [int] $WaitMinutes = 30,
    [switch] $WaitOnly,
    [int] $SinceMinutes = 60
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

function Get-RunHistory([string] $exportId) {
    # Query parameters go through --uri-parameters: a literal '&' in --url is split by az.cmd on Windows.
    $runs = az rest --method get --url "https://management.azure.com$exportId" `
        --uri-parameters "api-version=$api" '$expand=runHistory' `
        --query 'properties.runHistory.value[].properties' -o json | ConvertFrom-Json
    return @($runs | Where-Object { $_.submittedTime })
}

$expected = @{}
if ($WaitOnly) {
    $started = (Get-Date).ToUniversalTime().AddMinutes(-$SinceMinutes)
    foreach ($id in $exportIds) {
        $expected[$id] = @(Get-RunHistory $id | Where-Object { ([datetime]$_.submittedTime).ToUniversalTime() -ge $started }).Count
    }
    Write-Host "`n=== Waiting for runs submitted in the last $SinceMinutes minutes ===" -ForegroundColor Cyan
} else {
Write-Host "`n=== Trigger $($exportIds.Count) export(s) '$($o.COST_EXPORT_NAME)' ===" -ForegroundColor Cyan
$started = (Get-Date).ToUniversalTime().AddMinutes(-1)
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
}

Write-Host "`n=== Wait for the runs to finish (usually 2-10 minutes per export) ===" -ForegroundColor Cyan
$deadline = (Get-Date).AddMinutes($WaitMinutes)
$failed = @()
do {
    Start-Sleep -Seconds 30
    $pending = 0
    $failed = @()
    $line = foreach ($id in $exportIds) {
        $history = Get-RunHistory $id
        $recent = @($history | Where-Object { ([datetime]$_.submittedTime).ToUniversalTime() -ge $started })
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
$files = az storage fs file list --account-name $o.AZURE_STORAGE_ACCOUNT_NAME --file-system costs --auth-mode login `
    --query "[?ends_with(name, '.parquet')].{name:name, size:contentLength}" -o json | ConvertFrom-Json
if ($LASTEXITCODE -ne 0) {
    Write-Warning 'Listing failed. New role assignments can take ~5 minutes to apply; retry, or check the container in the portal.'
} else {
    $files | ForEach-Object { '{0,8:N1} KB  {1}' -f ($_.size / 1KB), $_.name } | Write-Host
    Write-Host "  $(@($files).Count) parquet file(s)"
}
Write-Host "`nNext: python ./scripts/setup_fabric.py --capacity-name <your-capacity>" -ForegroundColor Green
