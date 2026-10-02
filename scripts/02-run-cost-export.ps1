<#
.SYNOPSIS
  Step 2 - Run the FOCUS cost export now (and optionally backfill previous months), then list the files.

.DESCRIPTION
  The scheduled export only starts tomorrow and only covers month-to-date. For a demo you usually want
  data immediately and some history, so this script calls the Exports "run" API:
    POST {scope}/providers/Microsoft.CostManagement/exports/{name}/run?api-version=2025-03-01
  with an optional { timePeriod: { from, to } } body for each month to backfill.

.EXAMPLE
  ./scripts/02-run-cost-export.ps1 -BackfillMonths 3
#>
[CmdletBinding()]
param(
    [int] $BackfillMonths = 0,
    [int] $WaitMinutes = 20
)
$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path $PSScriptRoot -Parent
$outputsFile = Join-Path $repoRoot '.azure-outputs.json'
if (-not (Test-Path $outputsFile)) { throw 'Run ./scripts/01-deploy-azure.ps1 first.' }
$o = Get-Content $outputsFile -Raw | ConvertFrom-Json

$api = '2025-03-01'
$exportUrl = "https://management.azure.com$($o.COST_EXPORT_ID)"

function Invoke-ExportRun($from, $to) {
    $label = if ($from) { "$($from.ToString('yyyy-MM-dd')) -> $($to.ToString('yyyy-MM-dd'))" } else { 'configured period (month to date)' }
    Write-Host "Running export for $label"
    $azArgs = @('rest', '--method', 'post', '--url', "$exportUrl/run?api-version=$api")
    if ($from) {
        $bodyFile = New-TemporaryFile
        @{ timePeriod = @{ from = $from.ToString('yyyy-MM-ddT00:00:00Z'); to = $to.ToString('yyyy-MM-ddT23:59:59Z') } } |
            ConvertTo-Json | Set-Content $bodyFile -Encoding utf8
        $azArgs += @('--body', "@$bodyFile")
    }
    az @azArgs | Out-Null
    if ($bodyFile) { Remove-Item $bodyFile -ErrorAction SilentlyContinue }
    if ($LASTEXITCODE -ne 0) { throw "Export run failed for $label" }
}

Write-Host "`n=== Trigger export '$($o.COST_EXPORT_NAME)' ===" -ForegroundColor Cyan
$started = (Get-Date).ToUniversalTime()
Invoke-ExportRun $null $null
$firstOfMonth = Get-Date -Day 1 -Hour 0 -Minute 0 -Second 0
for ($i = 1; $i -le $BackfillMonths; $i++) {
    $from = $firstOfMonth.AddMonths(-$i)
    Start-Sleep -Seconds 5   # stay below the Cost Management throttling limits
    Invoke-ExportRun $from $from.AddMonths(1).AddDays(-1)
}

Write-Host "`n=== Wait for the runs to finish (usually 2-10 minutes) ===" -ForegroundColor Cyan
$expected = 1 + $BackfillMonths
$deadline = (Get-Date).AddMinutes($WaitMinutes)
do {
    Start-Sleep -Seconds 30
    $history = az rest --method get --url "$exportUrl`?api-version=$api&`$expand=runHistory" --query 'properties.runHistory.value[].properties' -o json | ConvertFrom-Json
    $recent = @($history | Where-Object { [datetime]$_.submittedTime -ge $started.AddMinutes(-1) })
    $summary = ($recent | Group-Object status | ForEach-Object { "$($_.Name)=$($_.Count)" }) -join ', '
    Write-Host ("  {0:HH:mm:ss}  {1}" -f (Get-Date), $(if ($summary) { $summary } else { 'queued' }))
    $done = @($recent | Where-Object { $_.status -in 'Completed', 'Failed' }).Count
} while ($done -lt $expected -and (Get-Date) -lt $deadline)

$failed = @($recent | Where-Object status -eq 'Failed')
if ($failed) { $failed | ForEach-Object { Write-Warning "Run failed: $($_.error.message)" } }

Write-Host "`n=== Files in container 'costs' ===" -ForegroundColor Cyan
az storage fs file list --account-name $o.AZURE_STORAGE_ACCOUNT_NAME --file-system costs --auth-mode login `
    --query "[?ends_with(name, '.parquet')].{file:name, sizeKB:to_string(floor(contentLength/``1024``))}" -o table
if ($LASTEXITCODE -ne 0) {
    Write-Warning 'Listing failed. New role assignments can take ~5 minutes to apply; retry, or check the container in the portal.'
}
Write-Host "`nNext: python ./scripts/setup_fabric.py --capacity-name <your-capacity>" -ForegroundColor Green
