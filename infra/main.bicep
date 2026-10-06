// Azure Cost -> Fabric : landing zone for the Cost Management FOCUS export.
// Deploy at subscription scope:
//   az deployment sub create -l <region> -f infra/main.bicep -p infra/main.parameters.json
targetScope = 'subscription'

@description('Short environment name used in resource names (e.g. demo, prod).')
@minLength(2)
@maxLength(10)
param environmentName string

@description('Azure region for the resource group and storage account.')
param location string = deployment().location

@description('Resource group that will hold the landing-zone storage account.')
param resourceGroupName string = 'rg-costfabric-${environmentName}'

@description('Storage account name (3-24 lowercase letters/digits, globally unique). Empty = generated stcost<token>.')
@maxLength(24)
param storageAccountName string = ''

@description('Object ID of a user that should be able to browse the exported files and create the Fabric connection (Storage Blob Data Reader). Leave empty to skip.')
param readerUserObjectId string = ''

@description('Object ID (service principal) of the Fabric workspace identity. Leave empty; setup_fabric.py can grant this later.')
param fabricWorkspaceIdentityPrincipalId string = ''

@description('Date the daily export schedule starts (yyyy-MM-dd). Defaults to today; the schedule begins tomorrow.')
param exportStartDate string = utcNow('yyyy-MM-dd')

@description('Subscriptions to export (same tenant). Empty = only the deployment subscription. Each gets its own FOCUS export writing to focus/<subscriptionId>/ in the shared storage account.')
param exportSubscriptionIds array = []

@description('Set to false when a billing-account / billing-profile export (scripts/01-deploy-azure.ps1 -BillingScope) covers all subscriptions instead.')
param createSubscriptionExports bool = true

var resourceToken = toLower(uniqueString(subscription().id, environmentName))
var storageName = empty(storageAccountName) ? take('stcost${resourceToken}', 24) : toLower(storageAccountName)
var exportName = 'focus-daily-${environmentName}'
var exportSubscriptions = createSubscriptionExports
  ? (empty(exportSubscriptionIds) ? [subscription().subscriptionId] : exportSubscriptionIds)
  : []
var tags = {
  environment: environmentName
  project: 'azure-cost-to-fabric'
}

resource rg 'Microsoft.Resources/resourceGroups@2024-03-01' = {
  name: resourceGroupName
  location: location
  tags: tags
}

module storage 'modules/storage.bicep' = {
  scope: rg
  name: 'costLandingStorage'
  params: {
    storageAccountName: storageName
    location: location
    tags: tags
    readerUserObjectId: readerUserObjectId
    fabricWorkspaceIdentityPrincipalId: fabricWorkspaceIdentityPrincipalId
  }
}

module costExports 'modules/cost-export.bicep' = [for subscriptionId in exportSubscriptions: {
  name: take('focusCostExport-${subscriptionId}', 64)
  scope: subscription(subscriptionId)
  params: {
    exportName: exportName
    storageAccountId: storage.outputs.storageAccountId
    rootFolderPath: 'focus/${subscriptionId}'
    scheduleStart: dateTimeAdd('${exportStartDate}T00:00:00Z', 'P1D')
  }
}]

output AZURE_RESOURCE_GROUP string = rg.name
output AZURE_STORAGE_ACCOUNT_NAME string = storage.outputs.storageAccountName
output AZURE_STORAGE_ACCOUNT_ID string = storage.outputs.storageAccountId
output AZURE_STORAGE_DFS_URL string = storage.outputs.dfsEndpoint
output COST_EXPORT_NAME string = exportName
output COST_EXPORT_IDS array = [for (subscriptionId, i) in exportSubscriptions: costExports[i].outputs.exportId]
