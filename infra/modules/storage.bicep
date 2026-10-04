// ADLS Gen2 account that receives the Cost Management export (container: costs).

param storageAccountName string
param location string
param tags object = {}

@description('Optional user object ID granted Storage Blob Data Reader.')
param readerUserObjectId string = ''

@description('Optional Fabric workspace identity (service principal) granted Storage Blob Data Reader.')
param fabricWorkspaceIdentityPrincipalId string = ''

var storageBlobDataReaderRoleId = '2a2b9908-6ea1-4ae2-8e65-a410df84e7d1'

resource storageAccount 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: storageAccountName
  location: location
  tags: tags
  kind: 'StorageV2'
  sku: {
    name: 'Standard_LRS'
  }
  properties: {
    accessTier: 'Hot'
    isHnsEnabled: true // ADLS Gen2: required for OneLake ADLS shortcuts
    minimumTlsVersion: 'TLS1_2'
    supportsHttpsTrafficOnly: true
    allowBlobPublicAccess: false
    allowSharedKeyAccess: false // exports use a managed identity; Fabric uses workspace identity
    networkAcls: {
      defaultAction: 'Allow'
      bypass: 'AzureServices'
    }
  }
}

resource blobService 'Microsoft.Storage/storageAccounts/blobServices@2023-05-01' = {
  parent: storageAccount
  name: 'default'
  properties: {
    deleteRetentionPolicy: {
      enabled: true
      days: 7
    }
  }
}

resource costsContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = {
  parent: blobService
  name: 'costs'
  properties: {
    publicAccess: 'None'
  }
}

resource userReader 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (!empty(readerUserObjectId)) {
  name: guid(storageAccount.id, readerUserObjectId, storageBlobDataReaderRoleId)
  scope: storageAccount
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', storageBlobDataReaderRoleId)
    principalId: readerUserObjectId
    principalType: 'User'
  }
}

resource workspaceIdentityReader 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (!empty(fabricWorkspaceIdentityPrincipalId)) {
  name: guid(storageAccount.id, fabricWorkspaceIdentityPrincipalId, storageBlobDataReaderRoleId)
  scope: storageAccount
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', storageBlobDataReaderRoleId)
    principalId: fabricWorkspaceIdentityPrincipalId
    principalType: 'ServicePrincipal'
  }
}

output storageAccountId string = storageAccount.id
output storageAccountName string = storageAccount.name
output dfsEndpoint string = storageAccount.properties.primaryEndpoints.dfs
