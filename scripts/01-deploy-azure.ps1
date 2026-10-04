<#
.SYNOPSIS
  Step 1 - Deploy the Azure side: resource group, ADLS Gen2 storage and the daily FOCUS cost export(s).

.DESCRIPTION
  Three ways to choose which subscriptions are covered:
    (default)               only the current subscription
    -ExportSubscriptionIds  one FOCUS export per listed subscription, all writing to the same storage
    -AllSubscriptions       same, for every enabled subscription in the current tenant
    -BillingScope           ONE export at EA billing account / MCA billing profile scope that already
                            contains every subscription billed there (needs billing-account permissions)

  Storage always lives in the current (or -SubscriptionId) subscription. Each export writes to
  costs/focus/<subscriptionId>/ (or costs/focus/billing/ for -BillingScope).

.EXAMPLE
  ./scripts/01-deploy-azure.ps1 -EnvironmentName demo
  ./scripts/01-deploy-azure.ps1 -EnvironmentName demo -ExportSubscriptionIds <sub1>,<sub2>,<sub3>
  ./scripts/01-deploy-azure.ps1 -EnvironmentName demo -AllSubscriptions
  ./scripts/01-deploy-azure.ps1 -EnvironmentName demo -BillingScope /providers/Microsoft.Billing/billingAccounts/<id>
#>
[CmdletBinding(DefaultParameterSetName = 'Subscriptions')]
param(
    [Parameter(Mandatory)] [ValidateLength(2, 10)] [string] $EnvironmentName,
    [string] $Location = 'westeurope',
    [string] $SubscriptionId,
    [Parameter(ParameterSetName = 'Subscriptions')] [string[]] $ExportSubscriptionIds,
    [Parameter(ParameterSetName = 'All')] [switch] $AllSubscriptions,
    [Parameter(ParameterSetName = 'Billing')] [string] $BillingScope
)
$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path $PSScriptRoot -Parent
$api = '2025-03-01'

function Step($text) { Write-Host "`n=== $text ===" -ForegroundColor Cyan }

function Register-Providers([string] $sub, [string[]] $namespaces) {
    foreach ($ns in $namespaces) {
        $state = az provider show --namespace $ns --subscription $sub --query registrationState -o tsv
        if ($state -ne 'Registered') {
            Write-Host "  [$sub] registering $ns ..."
            az provider register --namespace $ns --subscription $sub --wait | Out-Null
        }
    }
    Write-Host "  [$sub] $($namespaces -join ', ') : Registered"
}

Step '1/5 Azure sign-in and subscriptions'
if ($SubscriptionId) { az account set --subscription $SubscriptionId | Out-Null }
$account = az account show --query '{name:name, id:id, user:user.name, tenantId:tenantId}' -o json | ConvertFrom-Json
if (-not $account) { throw 'Run "az login" first.' }
Write-Host "Storage subscription: $($account.name) ($($account.id))  User: $($account.user)"

$tenantSubs = az account list --query "[?state=='Enabled' && tenantId=='$($account.tenantId)'].{id:id, name:name}" -o json | ConvertFrom-Json
$exportSubs = @()
switch ($PSCmdlet.ParameterSetName) {
    'All' { $exportSubs = @($tenantSubs.id) }
    'Billing' { $exportSubs = @() }
    default { $exportSubs = if ($ExportSubscriptionIds) { @($ExportSubscriptionIds) } else { @($account.id) } }
}
$foreign = @($exportSubs | Where-Object { $_ -notin $tenantSubs.id })
if ($foreign) {
    Write-Warning "Skipping subscriptions that are not enabled in tenant $($account.tenantId) (exports cannot write across tenants): $($foreign -join ', ')"
    $exportSubs = @($exportSubs | Where-Object { $_ -in $tenantSubs.id })
}
if ($PSCmdlet.ParameterSetName -eq 'Billing') {
    Write-Host "Export scope: billing scope $BillingScope (all subscriptions billed there)"
} else {
    if (-not $exportSubs) { throw 'No subscriptions to export.' }
    Write-Host "Export scope: $($exportSubs.Count) subscription(s)"
    $tenantSubs | Where-Object { $_.id -in $exportSubs } | ForEach-Object { Write-Host "  - $($_.name) ($($_.id))" }
}

Step '2/5 Register resource providers'
Register-Providers $account.id @('Microsoft.Storage', 'Microsoft.CostManagementExports')
foreach ($sub in $exportSubs | Where-Object { $_ -ne $account.id }) {
    Register-Providers $sub @('Microsoft.CostManagementExports')
}

Step '3/5 Deploy infra/main.bicep (subscription scope)'
$userObjectId = az ad signed-in-user show --query id -o tsv 2>$null
$paramsFile = New-TemporaryFile
@{
    '$schema'      = 'https://schema.management.azure.com/schemas/2019-04-01/deploymentParameters.json#'
    contentVersion = '1.0.0.0'
    parameters     = @{
        environmentName           = @{ value = $EnvironmentName }
        location                  = @{ value = $Location }
        readerUserObjectId        = @{ value = "$userObjectId" }
        exportSubscriptionIds     = @{ value = @($exportSubs) }
        createSubscriptionExports = @{ value = ($PSCmdlet.ParameterSetName -ne 'Billing') }
    }
} | ConvertTo-Json -Depth 5 | Set-Content $paramsFile -Encoding utf8
try {
    $outputs = az deployment sub create `
        --name "costfabric-$EnvironmentName" `
        --location $Location `
        --template-file (Join-Path $repoRoot 'infra/main.bicep') `
        --parameters "@$paramsFile" `
        --query properties.outputs -o json | ConvertFrom-Json
    if ($LASTEXITCODE -ne 0) { throw 'Deployment failed.' }
} finally { Remove-Item $paramsFile -ErrorAction SilentlyContinue }

$flat = [ordered]@{}
foreach ($p in $outputs.PSObject.Properties) { $flat[$p.Name] = $p.Value.value }

Step '4/5 Billing-scope export'
if ($PSCmdlet.ParameterSetName -eq 'Billing') {
    $scope = $BillingScope.TrimEnd('/')
    $exportId = "$scope/providers/Microsoft.CostManagement/exports/$($flat.COST_EXPORT_NAME)"
    $start = (Get-Date).ToUniversalTime().Date.AddDays(1)
    $body = @{
        properties = @{
            exportDescription     = 'Daily FOCUS cost export consumed by Microsoft Fabric (billing scope)'
            definition            = @{ type = 'FocusCost'; timeframe = 'MonthToDate'; dataSet = @{ granularity = 'Daily'; configuration = @{ dataVersion = '1.0' } } }
            deliveryInfo          = @{ destination = @{ type = 'AzureBlob'; resourceId = $flat.AZURE_STORAGE_ACCOUNT_ID; container = 'costs'; rootFolderPath = 'focus/billing' } }
            format                = 'Parquet'
            compressionMode       = 'snappy'
            partitionData         = $true
            dataOverwriteBehavior = 'OverwritePreviousReport'
            schedule              = @{ status = 'Active'; recurrence = 'Daily'; recurrencePeriod = @{ from = $start.ToString('yyyy-MM-ddTHH:mm:ssZ'); to = $start.AddYears(5).ToString('yyyy-MM-ddTHH:mm:ssZ') } }
        }
    }
    $bodyFile = New-TemporaryFile
    $body | ConvertTo-Json -Depth 10 | Set-Content $bodyFile -Encoding utf8
    try {
        az rest --method put --url "https://management.azure.com$exportId`?api-version=$api" --body "@$bodyFile" | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "Could not create the billing-scope export. You need Billing account contributor (EA: Enterprise administrator) on $scope." }
    } finally { Remove-Item $bodyFile -ErrorAction SilentlyContinue }
    $flat['COST_EXPORT_IDS'] = @($exportId)
    Write-Host "Created $exportId"
} else {
    Write-Host 'Not used (subscription exports were created by Bicep).'
}

Step '5/5 Save outputs to .azure-outputs.json'
$flat['AZURE_SUBSCRIPTION_ID'] = $account.id
$flat | ConvertTo-Json -Depth 5 | Set-Content (Join-Path $repoRoot '.azure-outputs.json') -Encoding utf8
$flat.GetEnumerator() | ForEach-Object {
    $value = if ($_.Value -is [array]) { "$($_.Value.Count) export(s)`n" + (($_.Value | ForEach-Object { "      $_" }) -join "`n") } else { $_.Value }
    '{0,-28} {1}' -f $_.Key, $value
} | Write-Host

Write-Host "`nDone. Next: ./scripts/02-run-cost-export.ps1 -BackfillMonths 3" -ForegroundColor Green
