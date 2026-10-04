// Daily FOCUS 1.0 cost export (month-to-date) at subscription scope.
targetScope = 'subscription'

param exportName string
param storageAccountId string

@description('Folder inside the "costs" container. One folder per export keeps subscriptions apart.')
param rootFolderPath string = 'focus/${subscription().subscriptionId}'

@description('First scheduled run (ISO 8601). Must be in the future.')
param scheduleStart string

resource costExport 'Microsoft.CostManagement/exports@2025-03-01' = {
  name: exportName
  // Managed identity lets the export write with Entra ID auth, so the storage account can keep shared key access disabled.
  // Cost Management grants it Storage Blob Data Contributor on the container (deployer needs roleAssignments/write).
  location: 'global'
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    exportDescription: 'Daily FOCUS cost export consumed by Microsoft Fabric'
    definition: {
      type: 'FocusCost'
      timeframe: 'MonthToDate'
      dataSet: {
        granularity: 'Daily'
        configuration: {
          dataVersion: '1.0'
        }
      }
    }
    deliveryInfo: {
      destination: {
        type: 'AzureBlob'
        resourceId: storageAccountId
        container: 'costs'
        rootFolderPath: rootFolderPath
      }
    }
    format: 'Parquet'
    compressionMode: 'snappy'
    partitionData: true
    // Each daily run replaces the month's files; Silver still keeps only the latest run as a safeguard.
    dataOverwriteBehavior: 'OverwritePreviousReport'
    schedule: {
      status: 'Active'
      recurrence: 'Daily'
      recurrencePeriod: {
        from: scheduleStart
        to: dateTimeAdd(scheduleStart, 'P5Y')
      }
    }
  }
}

output exportName string = costExport.name
output exportId string = costExport.id
output exportPrincipalId string = costExport.identity.principalId
