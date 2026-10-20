#Requires -Version 7.0
$ErrorActionPreference = 'Stop'
$target = Join-Path $PSScriptRoot '..\03-set-cost-export-schedule.ps1'
$fixture = New-TemporaryFile
$sub = '11111111-1111-1111-1111-111111111111'
$otherSub = '22222222-2222-2222-2222-222222222222'
$id = "/subscriptions/$sub/providers/Microsoft.CostManagement/exports/focus-daily-prod"
$billing = '/providers/Microsoft.Billing/billingAccounts/test'

function Reset-Mock {
    $global:CostExportTestState = @{
        Calls = [System.Collections.Generic.List[object]]::new()
        Writes = [System.Collections.Generic.List[object]]::new()
        Updated = @{}
        Pages = @{}
        FailUrl = ''
        FailMethod = ''
        IgnoreUpdate = $false
        InitialStatus = 'Active'
        End = '2099-01-01T00:00:00Z'
        DefinitionType = 'FocusCost'
        ETag = 'version-1'
    }
    @{ COST_EXPORT_IDS = @($id, $id); COST_EXPORT_ID = $id } |
        ConvertTo-Json | Set-Content $fixture -Encoding utf8NoBOM
}

function New-Export([string] $ResourceId) {
    $status = $global:CostExportTestState.InitialStatus
    if ($global:CostExportTestState.Updated.ContainsKey($ResourceId)) {
        $status = $global:CostExportTestState.Updated[$ResourceId]
    }
    @{
        id = $ResourceId
        name = 'focus-daily-prod'
        type = 'Microsoft.CostManagement/exports'
        eTag = $global:CostExportTestState.ETag
        location = 'global'
        identity = @{ type = 'SystemAssigned'; principalId = 'read-only'; tenantId = 'tenant-a' }
        properties = @{
            definition = @{
                type = $global:CostExportTestState.DefinitionType
                timeframe = 'MonthToDate'
                dataSet = @{ granularity = 'Daily'; configuration = @{ dataVersion = '1.0' } }
            }
            deliveryInfo = @{ destination = @{ resourceId = '/storage/original'; container = 'costs'; rootFolderPath = 'focus/original' } }
            schedule = @{
                status = $status
                recurrence = 'Daily'
                recurrencePeriod = @{ from = '2026-01-01T00:00:00Z'; to = $global:CostExportTestState.End }
            }
            format = 'Parquet'
            compressionMode = 'snappy'
            partitionData = $false
            dataOverwriteBehavior = 'OverwritePreviousReport'
            exportDescription = 'Keep this description'
            nextRunTimeEstimate = 'read-only'
            runHistory = @{ value = @() }
        }
    }
}

function az {
    $a = @($args)
    $global:LASTEXITCODE = 0
    $global:CostExportTestState.Calls.Add($a)
    if ($a[0] -eq 'account') {
        if ($a[1] -eq 'show') { return '{"tenantId":"tenant-a"}' }
        return (@(
            @{ id = $sub; name = 'Enabled in current tenant'; tenantId = 'tenant-a'; state = 'Enabled' },
            @{ id = $otherSub; name = 'Different tenant'; tenantId = 'tenant-b'; state = 'Enabled' },
            @{ id = 'disabled'; name = 'Disabled'; tenantId = 'tenant-a'; state = 'Disabled' }
        ) | ConvertTo-Json)
    }
    $method = $a[[array]::IndexOf($a, '--method') + 1]
    $url = $a[[array]::IndexOf($a, '--url') + 1]
    if ($url -eq $global:CostExportTestState.FailUrl -and
        (-not $global:CostExportTestState.FailMethod -or $method -eq $global:CostExportTestState.FailMethod)) {
        $global:LASTEXITCODE = 1
        return
    }
    if ($global:CostExportTestState.Pages.ContainsKey($url)) {
        return ($global:CostExportTestState.Pages[$url] | ConvertTo-Json -Depth 30)
    }
    $resourceId = ([uri]$url).AbsolutePath
    if ($method -eq 'put') {
        $path = $a[[array]::IndexOf($a, '--body') + 1].Substring(1)
        $body = Get-Content $path -Raw | ConvertFrom-Json -AsHashtable
        $global:CostExportTestState.Writes.Add(@{ Id = $resourceId; Body = $body; File = $path })
        if (-not $global:CostExportTestState.IgnoreUpdate) {
            $global:CostExportTestState.Updated[$resourceId] = $body.properties.schedule.status
        }
    }
    return (New-Export $resourceId | ConvertTo-Json -Depth 30)
}

function Assert([bool] $Condition, [string] $Message) {
    if (-not $Condition) { throw "Assertion failed: $Message" }
}

function Assert-Throws([scriptblock] $Action, [string] $Pattern) {
    $caught = $false
    try { & $Action } catch {
        $caught = $true
        Assert ($_.Exception.Message -like $Pattern) "Unexpected error: $($_.Exception.Message)"
    }
    Assert $caught "Expected failure: $Pattern"
}

try {
    Reset-Mock
    & $target -OutputsFile $fixture -WhatIf *> $null
    Assert ($global:CostExportTestState.Writes.Count -eq 0) 'WhatIf must not write.'

    Reset-Mock
    & $target -OutputsFile $fixture *> $null
    Assert ($global:CostExportTestState.Writes.Count -eq 1) 'Configured duplicate IDs must update only once.'
    $write = $global:CostExportTestState.Writes[0]
    $body = $write.Body
    Assert ($body.properties.schedule.status -eq 'Inactive') 'Disable must request Inactive.'
    Assert ($body.eTag -eq 'version-1') 'Latest eTag must be sent.'
    Assert ($body.location -eq 'global' -and $body.identity.type -eq 'SystemAssigned') 'Identity must be retained.'
    Assert (-not $body.identity.ContainsKey('principalId')) 'Identity read-only fields must not be sent.'
    Assert (-not $body.ContainsKey('id') -and -not $body.properties.ContainsKey('runHistory') -and
        -not $body.properties.ContainsKey('nextRunTimeEstimate')) 'Read-only fields must not be sent.'
    Assert ($body.properties.deliveryInfo.destination.resourceId -eq '/storage/original') 'Destination must be retained.'
    Assert ($body.properties.definition.dataSet.configuration.dataVersion -eq '1.0') 'FOCUS version must be retained.'
    Assert ([datetimeoffset]$body.properties.schedule.recurrencePeriod.to -eq [datetimeoffset]'2099-01-01T00:00:00Z') 'Dates must not change.'
    Assert ($body.properties.partitionData -eq $false -and $body.properties.compressionMode -eq 'snappy' -and
        $body.properties.format -eq 'Parquet' -and $body.properties.dataOverwriteBehavior -eq 'OverwritePreviousReport') 'Delivery settings, including false, must be retained.'
    Assert (-not (Test-Path $write.File)) 'Temporary request body must be removed.'
    $gets = @($global:CostExportTestState.Calls | Where-Object { $_ -contains 'get' })
    Assert ($gets.Count -eq 2) 'Each write must be verified with a second GET.'

    Reset-Mock
    $global:CostExportTestState.InitialStatus = 'Inactive'
    & $target -OutputsFile $fixture *> $null
    Assert ($global:CostExportTestState.Writes.Count -eq 0) 'Already Inactive must be a no-op.'
    & $target -OutputsFile $fixture -Status Active *> $null
    Assert ($global:CostExportTestState.Writes[0].Body.properties.schedule.status -eq 'Active') 'Enable must work.'

    Reset-Mock
    $list = "https://management.azure.com/subscriptions/$sub/providers/Microsoft.CostManagement/exports"
    $page2 = "$list/page2"
    $secondId = "$id-second"
    $nonFocus = New-Export "$id-actual"
    $nonFocus.properties.definition.type = 'ActualCost'
    $global:CostExportTestState.Pages[$list] = @{ value = @((New-Export $id), $nonFocus); nextLink = "${page2}?api-version=2025-03-01&skiptoken=abc" }
    $global:CostExportTestState.Pages[$page2] = @{ value = @((New-Export $secondId)) }
    & $target -AllSubscriptions -WhatIf *> $null
    Assert ($global:CostExportTestState.Writes.Count -eq 0) 'Discovery preview must not write.'
    & $target -AllSubscriptions *> $null
    Assert ($global:CostExportTestState.Writes.Count -eq 2) 'All FOCUS exports across all pages must be updated.'
    Assert (-not (@($global:CostExportTestState.Calls | Where-Object { ($_ -join ' ') -match "$otherSub|disabled|$id-actual" }).Count)) 'Other tenants, disabled subscriptions and non-FOCUS exports must not be read or updated.'
    Assert (-not (@($global:CostExportTestState.Calls | Where-Object { ($_ -join ' ') -match '--url [^ ]*&' }).Count)) 'URLs must not contain cmd shell separators.'
    & $target -SubscriptionIds $sub *> $null
    Assert ($global:CostExportTestState.Writes.Count -eq 2) 'Explicit subscriptions must also be idempotent.'

    Reset-Mock
    $global:CostExportTestState.Pages["https://management.azure.com$billing/providers/Microsoft.CostManagement/exports"] = @{
        value = @((New-Export "$billing/providers/Microsoft.CostManagement/exports/focus"))
    }
    & $target -Scope $billing *> $null
    Assert ($global:CostExportTestState.Writes.Count -eq 1) 'Billing scope discovery must work.'
    Assert ($global:CostExportTestState.Writes[0].Id.StartsWith($billing)) 'Billing export must be targeted.'

    Reset-Mock
    $global:CostExportTestState.DefinitionType = 'ActualCost'
    & $target -OutputsFile $fixture *> $null
    Assert ($global:CostExportTestState.Writes.Count -eq 0) 'Non-FOCUS configured IDs must be skipped.'

    Reset-Mock
    @{ COST_EXPORT_IDS = @($id, $secondId) } | ConvertTo-Json | Set-Content $fixture -Encoding utf8NoBOM
    $global:CostExportTestState.FailUrl = "https://management.azure.com$secondId"
    Assert-Throws { & $target -OutputsFile $fixture *> $null } '*Azure CLI failed*'
    Assert ($global:CostExportTestState.Writes.Count -eq 0) 'Failed later preflight must prevent all writes.'

    Reset-Mock
    $global:CostExportTestState.FailUrl = "https://management.azure.com$id"
    $global:CostExportTestState.FailMethod = 'put'
    Assert-Throws { & $target -OutputsFile $fixture *> $null } '*Azure CLI failed*'
    $put = @($global:CostExportTestState.Calls | Where-Object { $_ -contains 'put' })[0]
    $bodyFile = $put[[array]::IndexOf($put, '--body') + 1].Substring(1)
    Assert (-not (Test-Path $bodyFile)) 'PUT failure must also clean up.'

    Reset-Mock
    $global:CostExportTestState.IgnoreUpdate = $true
    Assert-Throws { & $target -OutputsFile $fixture *> $null } '*could not be verified*'
    Assert (-not (Test-Path $global:CostExportTestState.Writes[0].File)) 'Verification failure must also clean up.'

    Reset-Mock
    $global:CostExportTestState.InitialStatus = 'Inactive'
    $global:CostExportTestState.End = '2020-01-01T00:00:00Z'
    Assert-Throws { & $target -OutputsFile $fixture -Status Active *> $null } '*expired*'
    Assert ($global:CostExportTestState.Writes.Count -eq 0) 'Expired schedule must not be enabled.'

    Reset-Mock
    $global:CostExportTestState.ETag = ''
    Assert-Throws { & $target -OutputsFile $fixture *> $null } '*missing eTag*'
    Assert ($global:CostExportTestState.Writes.Count -eq 0) 'Missing eTag must prevent writes.'

    Reset-Mock
    $global:CostExportTestState.Pages[$list] = @{ value = @((New-Export $id)); nextLink = 'https://example.invalid/exports' }
    Assert-Throws { & $target -AllSubscriptions *> $null } '*Unexpected ARM URL*'
    Assert ($global:CostExportTestState.Writes.Count -eq 0) 'Unsafe pagination URL must prevent all writes.'

    Reset-Mock
    $global:CostExportTestState.Pages[$list] = @{ value = @(); nextLink = "${list}?api-version=2025-03-01" }
    Assert-Throws { & $target -AllSubscriptions *> $null } '*Repeated export-list page*'
    Assert ($global:CostExportTestState.Writes.Count -eq 0) 'Repeated pagination must fail before writes.'

    Reset-Mock
    $global:CostExportTestState.Pages[$list] = @{ value = @() }
    & $target -AllSubscriptions *> $null
    Assert ($global:CostExportTestState.Writes.Count -eq 0) 'Empty discovery must not write.'

    Write-Host 'PASS: preview, preservation, deduplication, verification, idempotency, enable, discovery, pagination, scope and error handling.'
} finally {
    Remove-Item -LiteralPath $fixture -ErrorAction Stop
    Remove-Variable -Name CostExportTestState -Scope Global -ErrorAction SilentlyContinue
}
