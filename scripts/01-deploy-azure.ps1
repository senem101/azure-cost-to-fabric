<#
.SYNOPSIS
  Step 1 - Deploy the Azure side: resource group, ADLS Gen2 storage and the daily FOCUS cost export.

.EXAMPLE
  ./scripts/01-deploy-azure.ps1 -EnvironmentName demo -Location westeurope
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)] [ValidateLength(2, 10)] [string] $EnvironmentName,
    [string] $Location = 'westeurope',
    [string] $SubscriptionId
)
$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path $PSScriptRoot -Parent

function Step($text) { Write-Host "`n=== $text ===" -ForegroundColor Cyan }

Step '1/4 Azure sign-in and subscription'
if ($SubscriptionId) { az account set --subscription $SubscriptionId | Out-Null }
$account = az account show --query '{name:name, id:id, user:user.name}' -o json | ConvertFrom-Json
if (-not $account) { throw 'Run "az login" first.' }
Write-Host "Subscription: $($account.name) ($($account.id))  User: $($account.user)"

Step '2/4 Register resource providers'
foreach ($ns in 'Microsoft.Storage', 'Microsoft.CostManagementExports') {
    $state = az provider show --namespace $ns --query registrationState -o tsv
    if ($state -ne 'Registered') {
        Write-Host "Registering $ns ..."
        az provider register --namespace $ns --wait | Out-Null
    }
    Write-Host "$ns : Registered"
}

Step '3/4 Deploy infra/main.bicep (subscription scope)'
$userObjectId = az ad signed-in-user show --query id -o tsv 2>$null
$deploymentName = "costfabric-$EnvironmentName"
$outputs = az deployment sub create `
    --name $deploymentName `
    --location $Location `
    --template-file (Join-Path $repoRoot 'infra/main.bicep') `
    --parameters environmentName=$EnvironmentName location=$Location readerUserObjectId=$userObjectId `
    --query properties.outputs -o json | ConvertFrom-Json
if ($LASTEXITCODE -ne 0) { throw 'Deployment failed.' }

Step '4/4 Save outputs to .azure-outputs.json'
$flat = [ordered]@{}
foreach ($p in $outputs.PSObject.Properties) { $flat[$p.Name] = $p.Value.value }
$flat['AZURE_SUBSCRIPTION_ID'] = $account.id
$flat | ConvertTo-Json | Set-Content (Join-Path $repoRoot '.azure-outputs.json') -Encoding utf8
$flat.GetEnumerator() | ForEach-Object { '{0,-28} {1}' -f $_.Key, $_.Value } | Write-Host

Write-Host "`nDone. Next: ./scripts/02-run-cost-export.ps1 -BackfillMonths 3" -ForegroundColor Green
