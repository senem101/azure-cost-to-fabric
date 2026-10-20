#Requires -Version 7.0
<#
.SYNOPSIS
  Disable (or re-enable) FOCUS export schedules without deleting exports or data.

.DESCRIPTION
  By default, reads COST_EXPORT_IDS / COST_EXPORT_ID from .azure-outputs.json.
  -AllSubscriptions discovers subscription-level FOCUS exports in enabled subscriptions
  visible to the signed-in user in the current tenant, including exports outside this repo.
  -SubscriptionIds limits discovery to selected subscriptions. -Scope supports explicit
  subscription, billing-account or billing-profile scopes.
  Discovery and validation finish before any writes. Updates preserve the export definition,
  destination, identity and schedule dates and use the latest eTag for concurrency control.
  Stops on an error; earlier successful updates are not rolled back.
  This does not cancel queued/running exports, block manual runs or stop Fabric pipelines.

.EXAMPLE
  .\scripts\03-set-cost-export-schedule.ps1 -WhatIf
  .\scripts\03-set-cost-export-schedule.ps1
  .\scripts\03-set-cost-export-schedule.ps1 -AllSubscriptions -WhatIf
  .\scripts\03-set-cost-export-schedule.ps1 -AllSubscriptions
  .\scripts\03-set-cost-export-schedule.ps1 -SubscriptionIds <id1>,<id2>
  .\scripts\03-set-cost-export-schedule.ps1 -Scope /providers/Microsoft.Billing/billingAccounts/<id>
  .\scripts\03-set-cost-export-schedule.ps1 -Status Active -WhatIf
#>
[CmdletBinding(SupportsShouldProcess, ConfirmImpact = 'Medium', DefaultParameterSetName = 'Outputs')]
param(
    [ValidateSet('Inactive', 'Active')] [string] $Status = 'Inactive',
    [Parameter(ParameterSetName = 'Outputs')]
    [string] $OutputsFile = (Join-Path (Split-Path $PSScriptRoot -Parent) '.azure-outputs.json'),
    [Parameter(Mandatory, ParameterSetName = 'All')] [switch] $AllSubscriptions,
    [Parameter(Mandatory, ParameterSetName = 'Subscriptions')]
    [ValidateNotNullOrEmpty()] [guid[]] $SubscriptionIds,
    [Parameter(Mandatory, ParameterSetName = 'Scopes')]
    [ValidateNotNullOrEmpty()] [string[]] $Scope
)
$ErrorActionPreference = 'Stop'
$api = '2025-03-01'
$arm = 'https://management.azure.com'

function Invoke-AzJson([string[]] $Arguments) {
    $json = az @Arguments --only-show-errors --output json
    if ($LASTEXITCODE -ne 0) {
        throw "Azure CLI failed (exit $LASTEXITCODE): az $($Arguments -join ' '). Check sign-in and scope permissions."
    }
    if (-not $json) { throw "Azure CLI returned no JSON: az $($Arguments -join ' ')." }
    return ($json -join "`n" | ConvertFrom-Json -AsHashtable)
}

function Invoke-Arm([string] $Method, [string] $Url, [string] $BodyFile) {
    $uri = [uri]$Url
    if ($uri.Scheme -ne 'https' -or $uri.Authority -ne 'management.azure.com') {
        throw "Unexpected ARM URL: $Url"
    }
    # Keep '&' out of --url: az.cmd on Windows can interpret it as a shell separator.
    $cliArgs = @('rest', '--method', $Method, '--url', $uri.GetLeftPart([System.UriPartial]::Path))
    if ($uri.Query) {
        $cliArgs += '--uri-parameters'
        $cliArgs += @($uri.Query.TrimStart('?').Split('&') | ForEach-Object { [uri]::UnescapeDataString($_) })
    }
    if ($BodyFile) { $cliArgs += @('--body', "@$BodyFile") }
    return (Invoke-AzJson $cliArgs)
}

function Get-ExportIds([string] $ExportScope) {
    if ($ExportScope -notmatch '^/(subscriptions/[^/]+|providers/Microsoft\.Billing/billingAccounts/[^/]+(/billingProfiles/[^/]+)?)$') {
        throw "Unsupported scope: $ExportScope"
    }
    $url = "$arm$ExportScope/providers/Microsoft.CostManagement/exports?api-version=$api"
    $seen = @{}
    do {
        if ($seen.ContainsKey($url)) { throw "Repeated export-list page: $url" }
        $seen[$url] = $true
        $page = Invoke-Arm 'get' $url
        if (-not $page.ContainsKey('value')) { throw "Export list is missing 'value': $url" }
        foreach ($export in $page.value) {
            if ($export.properties.definition.type -eq 'FocusCost') {
                if (-not $export.id) { throw "FOCUS export is missing its resource ID in scope $ExportScope." }
                $export.id
            }
        }
        $url = $page.nextLink
    } while ($url)
}

$account = Invoke-AzJson @('account', 'show')
if (-not $account.tenantId) { throw 'No current tenant. Run az login --tenant <TENANT_ID> first.' }
Write-Host "Tenant: $($account.tenantId) | Requested schedule status: $Status"

$ids = @()
switch ($PSCmdlet.ParameterSetName) {
    'Outputs' {
        if (-not (Test-Path -LiteralPath $OutputsFile -PathType Leaf)) {
            throw "Outputs file not found: $OutputsFile. Use -AllSubscriptions, -SubscriptionIds or -Scope instead."
        }
        $outputs = Get-Content -LiteralPath $OutputsFile -Raw | ConvertFrom-Json -AsHashtable
        $ids = @($outputs.COST_EXPORT_IDS) + @($outputs.COST_EXPORT_ID) | Where-Object { $_ }
        if (-not $ids) { throw "No COST_EXPORT_IDS / COST_EXPORT_ID in $OutputsFile." }
    }
    'All' {
        $subscriptions = Invoke-AzJson @('account', 'list', '--all', '--refresh')
        $visible = @($subscriptions | Where-Object { $_.tenantId -eq $account.tenantId -and $_.state -eq 'Enabled' })
        if (-not $visible) { throw "No enabled subscriptions visible in tenant $($account.tenantId)." }
        Write-Warning '-AllSubscriptions includes FOCUS exports outside this repo, but not billing-scope exports or subscriptions you cannot see.'
        foreach ($sub in $visible) {
            Write-Host "Discovering exports: $($sub.name) ($($sub.id))"
            $ids += @(Get-ExportIds "/subscriptions/$($sub.id)")
        }
    }
    'Subscriptions' {
        foreach ($sub in $SubscriptionIds) { $ids += @(Get-ExportIds "/subscriptions/$sub") }
    }
    'Scopes' {
        foreach ($item in $Scope) { $ids += @(Get-ExportIds $item.TrimEnd('/')) }
    }
}

$exports = @(foreach ($id in ($ids | Sort-Object -Unique)) {
    if ($id -notmatch '^/.+/providers/Microsoft\.CostManagement/exports/[^/?#]+$') {
        throw "Invalid export resource ID: $id"
    }
    $export = Invoke-Arm 'get' "$arm${id}?api-version=$api"
    if ($export.properties.definition.type -ne 'FocusCost') {
        Write-Warning "Skipping non-FOCUS export: $id"
        continue
    }
    $export['id'] = $id
    if (-not $export.eTag -or -not $export.properties.schedule.status -or -not $export.properties.deliveryInfo) {
        throw "Export is missing eTag, schedule or destination: $id. No updates have been made."
    }
    if ($Status -eq 'Active' -and $export.properties.schedule.status -ne 'Active') {
        $end = $export.properties.schedule.recurrencePeriod.to
        if (-not $end -or [datetimeoffset]$end -le [datetimeoffset]::UtcNow) {
            throw "Schedule has expired or has no end date: $id. Update its dates in Cost Management before enabling it."
        }
    }
    $export
})
if (-not $exports) {
    Write-Warning 'No FOCUS exports found in the selected scope. Nothing was changed.'
    return
}
$exports | ForEach-Object {
    [pscustomobject]@{ Current = $_.properties.schedule.status; Requested = $Status; Export = $_.id }
} | Format-Table -AutoSize -Wrap | Out-Host

foreach ($export in $exports) {
    $id = $export.id
    if ($export.properties.schedule.status -eq $Status) {
        Write-Host "Already $Status : $id"
        continue
    }
    if (-not $PSCmdlet.ShouldProcess($id, "Set FOCUS export schedule to $Status")) { continue }

    $properties = @{}
    foreach ($key in @('definition', 'deliveryInfo', 'schedule', 'format', 'compressionMode',
                       'partitionData', 'dataOverwriteBehavior', 'exportDescription')) {
        if ($export.properties.ContainsKey($key)) { $properties[$key] = $export.properties[$key] }
    }
    $properties.schedule.status = $Status
    $body = @{ eTag = $export.eTag; properties = $properties }
    if ($export.ContainsKey('location')) { $body.location = $export.location }
    if ($export.identity) { $body.identity = @{ type = $export.identity.type } }

    $file = New-TemporaryFile
    try {
        $body | ConvertTo-Json -Depth 100 | Set-Content -LiteralPath $file -Encoding utf8NoBOM
        $null = Invoke-Arm 'put' "$arm${id}?api-version=$api" $file
        $verified = Invoke-Arm 'get' "$arm${id}?api-version=$api"
        if ($verified.properties.schedule.status -ne $Status) {
            throw "Update could not be verified for $id. Expected $Status, got '$($verified.properties.schedule.status)'."
        }
        Write-Host "Verified $Status : $id" -ForegroundColor Green
    } finally {
        Remove-Item -LiteralPath $file -ErrorAction Stop
    }
}
Write-Host 'Only scheduled export runs are affected. Fabric schedules and queued/running exports are unchanged.'
