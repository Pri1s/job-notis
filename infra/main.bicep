@description('Azure region for every Job Notis resource.')
param location string = 'centralus'

@description('Discord webhook consumed by the Function App.')
@secure()
param discordWebhookUrl string

@description('Optional hardware Discord webhook; hardware delivery remains pending when blank.')
@secure()
param hardwareDiscordWebhookUrl string = ''

@description('Object ID of the signed-in user that performs initial state migration.')
param deployerPrincipalId string

@description('Keep the timer disabled until state migration and cutover are complete.')
param timerDisabled bool = true

var resourceToken = take(uniqueString(subscription().id, resourceGroup().id), 10)
var functionAppNameValue = 'func-job-notis-${resourceToken}'
var planName = 'plan-job-notis-${resourceToken}'
var storageName = 'stjobnotis${resourceToken}'
var logAnalyticsName = 'log-job-notis-${resourceToken}'
var appInsightsName = 'appi-job-notis-${resourceToken}'
var deploymentContainerName = 'function-releases'
var stateContainerNameValue = 'job-notis-state'
var stateBlobNameValue = 'state.sqlite3.gz'
var storageBlobDataContributorRoleId = 'ba92f5b4-2d11-453d-a403-e96b0029c9fe'
var storageConnectionString = 'DefaultEndpointsProtocol=https;AccountName=${storage.name};AccountKey=${storage.listKeys().keys[0].value};EndpointSuffix=${environment().suffixes.storage}'

resource logAnalytics 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: logAnalyticsName
  location: location
  properties: {
    retentionInDays: 30
    features: {
      searchVersion: 1
    }
    sku: {
      name: 'PerGB2018'
    }
  }
}

resource applicationInsights 'Microsoft.Insights/components@2020-02-02' = {
  name: appInsightsName
  location: location
  kind: 'web'
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: logAnalytics.id
  }
}

resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: storageName
  location: location
  kind: 'StorageV2'
  sku: {
    name: 'Standard_LRS'
  }
  properties: {
    accessTier: 'Hot'
    allowBlobPublicAccess: false
    allowSharedKeyAccess: true
    minimumTlsVersion: 'TLS1_2'
    publicNetworkAccess: 'Enabled'
    supportsHttpsTrafficOnly: true
    networkAcls: {
      bypass: 'AzureServices'
      defaultAction: 'Allow'
    }
  }

  resource blobService 'blobServices' = {
    name: 'default'
    properties: {
      deleteRetentionPolicy: {
        enabled: true
        days: 7
      }
      containerDeleteRetentionPolicy: {
        enabled: true
        days: 7
      }
    }

    resource deploymentContainer 'containers' = {
      name: deploymentContainerName
      properties: {
        publicAccess: 'None'
      }
    }

    resource stateContainer 'containers' = {
      name: stateContainerNameValue
      properties: {
        publicAccess: 'None'
      }
    }
  }
}

resource flexPlan 'Microsoft.Web/serverfarms@2024-04-01' = {
  name: planName
  location: location
  kind: 'functionapp'
  sku: {
    name: 'FC1'
    tier: 'FlexConsumption'
  }
  properties: {
    reserved: true
  }
}

resource functionApp 'Microsoft.Web/sites@2024-04-01' = {
  name: functionAppNameValue
  location: location
  kind: 'functionapp,linux'
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    serverFarmId: flexPlan.id
    httpsOnly: true
    publicNetworkAccess: 'Enabled'
    siteConfig: {
      alwaysOn: false
      ftpsState: 'Disabled'
      minTlsVersion: '1.2'
    }
    functionAppConfig: {
      deployment: {
        storage: {
          type: 'blobContainer'
          value: '${storage.properties.primaryEndpoints.blob}${deploymentContainerName}'
          authentication: {
            type: 'StorageAccountConnectionString'
            storageAccountConnectionStringName: 'AzureWebJobsStorage'
          }
        }
      }
      scaleAndConcurrency: {
        maximumInstanceCount: 40
        instanceMemoryMB: 2048
      }
      runtime: {
        name: 'python'
        version: '3.12'
      }
    }
  }

  resource appSettings 'config' = {
    name: 'appsettings'
    properties: {
      AzureWebJobsStorage: storageConnectionString
      'AzureWebJobs.job_notis_timer.Disabled': string(timerDisabled)
      FUNCTIONS_EXTENSION_VERSION: '~4'
      APPLICATIONINSIGHTS_CONNECTION_STRING: applicationInsights.properties.ConnectionString
      JOB_NOTIS_DISCORD_WEBHOOK_URL: discordWebhookUrl
      JOB_NOTIS_HARDWARE_DISCORD_WEBHOOK_URL: hardwareDiscordWebhookUrl
      JOB_NOTIS_STORAGE_ACCOUNT_URL: storage.properties.primaryEndpoints.blob
      JOB_NOTIS_STATE_CONTAINER: stateContainerNameValue
      JOB_NOTIS_STATE_BLOB: stateBlobNameValue
      JOB_NOTIS_TIMER_SCHEDULE: '0 7,22,37,52 * * * *'
    }
  }
}

resource functionStateRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(functionApp.id, storage::blobService::stateContainer.id, storageBlobDataContributorRoleId)
  scope: storage::blobService::stateContainer
  properties: {
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      storageBlobDataContributorRoleId
    )
    principalId: functionApp.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

resource deployerStateRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(deployerPrincipalId, storage::blobService::stateContainer.id, storageBlobDataContributorRoleId)
  scope: storage::blobService::stateContainer
  properties: {
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      storageBlobDataContributorRoleId
    )
    principalId: deployerPrincipalId
    principalType: 'User'
  }
}

output functionAppName string = functionApp.name
output storageAccountName string = storage.name
output storageAccountUrl string = storage.properties.primaryEndpoints.blob
output stateContainerName string = stateContainerNameValue
output stateBlobName string = stateBlobNameValue
output timerDisabled bool = timerDisabled
