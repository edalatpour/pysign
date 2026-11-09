targetScope = 'resourceGroup'

@description('Lowercase resource prefix, starting with a letter. Reuse it for runtime.bicep.')
@minLength(2)
@maxLength(8)
param prefix string = 'pisign'
param location string = resourceGroup().location

@allowed(['postgresql', 'mssql'])
param databaseEngine string = 'postgresql'
param sqlServerName string = ''
param sqlServerResourceGroup string = ''
param sqlDatabaseName string = ''
param existingEnvironmentName string = ''
param existingEnvironmentResourceGroup string = ''
param existingRegistryName string = ''
param existingRegistryResourceGroup string = ''

@secure()
param postgresAdminPassword string = ''

@secure()
@minLength(50)
param djangoSecretKey string

@secure()
param entraClientSecret string

param postgresAdminLogin string = 'pisignadmin'

var suffix = uniqueString(resourceGroup().id, prefix)
var registryName = '${replace(prefix, '-', '')}${suffix}'
var vaultName = '${prefix}-${suffix}'
var postgresName = '${prefix}-${suffix}-pg'
var reuseEnvironment = !empty(existingEnvironmentName)

resource network 'Microsoft.Network/virtualNetworks@2024-05-01' = if (!reuseEnvironment) {
  name: '${prefix}-vnet'
  location: location
  properties: {
    addressSpace: {
      addressPrefixes: ['10.42.0.0/16']
    }
    subnets: [
      {
        name: 'container-apps'
        properties: {
          addressPrefix: '10.42.0.0/23'
          delegations: [
            {
              name: 'container-apps'
              properties: { serviceName: 'Microsoft.App/environments' }
            }
          ]
        }
      }
      databaseEngine == 'postgresql' ? {
        name: 'postgresql'
        properties: {
          addressPrefix: '10.42.2.0/24'
          delegations: [
            {
              name: 'postgresql'
              properties: { serviceName: 'Microsoft.DBforPostgreSQL/flexibleServers' }
            }
          ]
        }
      } : {
        name: 'sql-private-endpoint'
        properties: {
          addressPrefix: '10.42.2.0/24'
          privateEndpointNetworkPolicies: 'Disabled'
        }
      }
    ]
  }
}

resource postgresDns 'Microsoft.Network/privateDnsZones@2024-06-01' = if (!reuseEnvironment && databaseEngine == 'postgresql') {
  name: '${prefix}.postgres.database.azure.com'
  location: 'global'
}

resource postgresDnsLink 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2024-06-01' = if (!reuseEnvironment && databaseEngine == 'postgresql') {
  parent: postgresDns
  name: '${prefix}-link'
  location: 'global'
  properties: {
    registrationEnabled: false
    virtualNetwork: { id: network!.id }
  }
}

resource postgres 'Microsoft.DBforPostgreSQL/flexibleServers@2024-08-01' = if (!reuseEnvironment && databaseEngine == 'postgresql') {
  name: postgresName
  location: location
  sku: {
    name: 'Standard_B1ms'
    tier: 'Burstable'
  }
  properties: {
    version: '17'
    administratorLogin: postgresAdminLogin
    administratorLoginPassword: postgresAdminPassword
    storage: { storageSizeGB: 32 }
    backup: {
      backupRetentionDays: 7
      geoRedundantBackup: 'Disabled'
    }
    highAvailability: { mode: 'Disabled' }
    network: {
      delegatedSubnetResourceId: network!.properties.subnets[1].id
      privateDnsZoneArmResourceId: postgresDns!.id
      publicNetworkAccess: 'Disabled'
    }
  }
  dependsOn: [postgresDnsLink]
}

resource database 'Microsoft.DBforPostgreSQL/flexibleServers/databases@2024-08-01' = if (!reuseEnvironment && databaseEngine == 'postgresql') {
  parent: postgres
  name: 'pisign'
  properties: {
    charset: 'UTF8'
    collation: 'en_US.utf8'
  }
}

resource sqlServer 'Microsoft.Sql/servers@2023-08-01' existing = if (databaseEngine == 'mssql') {
  scope: resourceGroup(sqlServerResourceGroup)
  name: sqlServerName
}

resource sqlDns 'Microsoft.Network/privateDnsZones@2024-06-01' = if (!reuseEnvironment && databaseEngine == 'mssql') {
  name: 'privatelink.${az.environment().suffixes.sqlServerHostname}'
  location: 'global'
}

resource sqlDnsLink 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2024-06-01' = if (!reuseEnvironment && databaseEngine == 'mssql') {
  parent: sqlDns
  name: '${prefix}-sql-link'
  location: 'global'
  properties: {
    registrationEnabled: false
    virtualNetwork: { id: network!.id }
  }
}

resource sqlEndpoint 'Microsoft.Network/privateEndpoints@2024-05-01' = if (!reuseEnvironment && databaseEngine == 'mssql') {
  name: '${prefix}-sql-endpoint'
  location: location
  properties: {
    subnet: { id: '${network!.id}/subnets/sql-private-endpoint' }
    privateLinkServiceConnections: [
      {
        name: '${prefix}-sql'
        properties: {
          privateLinkServiceId: sqlServer!.id
          groupIds: ['sqlServer']
        }
      }
    ]
  }
}

resource sqlEndpointDns 'Microsoft.Network/privateEndpoints/privateDnsZoneGroups@2024-05-01' = if (!reuseEnvironment && databaseEngine == 'mssql') {
  parent: sqlEndpoint
  name: 'default'
  properties: {
    privateDnsZoneConfigs: [
      {
        name: 'sql'
        properties: { privateDnsZoneId: sqlDns!.id }
      }
    ]
  }
}

resource logs 'Microsoft.OperationalInsights/workspaces@2023-09-01' = if (!reuseEnvironment) {
  name: '${prefix}-logs'
  location: location
  properties: {
    sku: { name: 'PerGB2018' }
    retentionInDays: 30
  }
}

resource environment 'Microsoft.App/managedEnvironments@2025-01-01' = if (!reuseEnvironment) {
  name: '${prefix}-env'
  location: location
  properties: {
    vnetConfiguration: {
      infrastructureSubnetId: network!.properties.subnets[0].id
      internal: false
    }
    workloadProfiles: [
      {
        name: 'Consumption'
        workloadProfileType: 'Consumption'
      }
    ]
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logs!.properties.customerId
        sharedKey: logs!.listKeys().primarySharedKey
      }
    }
  }
}

resource sharedEnvironment 'Microsoft.App/managedEnvironments@2025-01-01' existing = if (reuseEnvironment) {
  scope: resourceGroup(existingEnvironmentResourceGroup)
  name: existingEnvironmentName
}

resource registry 'Microsoft.ContainerRegistry/registries@2023-07-01' = if (empty(existingRegistryName)) {
  name: registryName
  location: location
  sku: { name: 'Basic' }
  properties: { adminUserEnabled: false }
}

resource identity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: '${prefix}-identity'
  location: location
}

resource documentIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: '${prefix}-documents-identity'
  location: location
}

module documentRegistryAccess './registry-access.bicep' = {
  scope: resourceGroup(empty(existingRegistryName) ? resourceGroup().name : existingRegistryResourceGroup)
  params: {
    registryName: empty(existingRegistryName) ? registry!.name : existingRegistryName
    identityResourceId: documentIdentity.id
    principalId: documentIdentity.properties.principalId
  }
}

resource assetStorage 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: 'ps${suffix}assets'
  location: location
  kind: 'StorageV2'
  sku: { name: 'Standard_LRS' }
  properties: {
    supportsHttpsTrafficOnly: true
    minimumTlsVersion: 'TLS1_2'
    allowBlobPublicAccess: false
    allowSharedKeyAccess: false
    defaultToOAuthAuthentication: true
  }
}

resource assetBlobs 'Microsoft.Storage/storageAccounts/blobServices@2023-05-01' = {
  parent: assetStorage
  name: 'default'
}

resource assetContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = {
  parent: assetBlobs
  name: 'assets'
  properties: { publicAccess: 'None' }
}

resource assetAccess 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(assetContainer.id, identity.id, 'StorageBlobDataContributor')
  scope: assetContainer
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', 'ba92f5b4-2d11-453d-a403-e96b0029c9fe')
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

module registryAccess './registry-access.bicep' = {
  scope: resourceGroup(empty(existingRegistryName) ? resourceGroup().name : existingRegistryResourceGroup)
  params: {
    registryName: empty(existingRegistryName) ? registry!.name : existingRegistryName
    identityResourceId: identity.id
    principalId: identity.properties.principalId
  }
}

resource vault 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: vaultName
  location: location
  properties: {
    tenantId: tenant().tenantId
    sku: { family: 'A', name: 'standard' }
    enableRbacAuthorization: true
    enableSoftDelete: true
    enablePurgeProtection: true
    softDeleteRetentionInDays: 7
    accessPolicies: []
  }
}

resource vaultReader 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(vault.id, identity.id, 'KeyVaultSecretsUser')
  scope: vault
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '4633458b-17de-408a-b874-0445c86b69e6')
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

resource databasePassword 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = if (databaseEngine == 'postgresql') {
  parent: vault
  name: 'postgres-password'
  properties: { value: postgresAdminPassword }
}

resource sessionSecret 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: vault
  name: 'django-secret-key'
  properties: { value: djangoSecretKey }
}

resource entraSecret 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: vault
  name: 'entra-client-secret'
  properties: { value: entraClientSecret }
}

output registryName string = registryAccess.outputs.registryName
output registryResourceGroup string = empty(existingRegistryName) ? resourceGroup().name : existingRegistryResourceGroup
output registryLoginServer string = registryAccess.outputs.registryLoginServer
output environmentName string = reuseEnvironment ? sharedEnvironment!.name : environment!.name
output environmentResourceGroup string = reuseEnvironment ? existingEnvironmentResourceGroup : resourceGroup().name
output workloadProfileName string = reuseEnvironment && empty(sharedEnvironment!.properties.?workloadProfiles) ? '' : 'Consumption'
output postgresHost string = databaseEngine == 'postgresql' ? postgres!.properties.fullyQualifiedDomainName : ''
output postgresUser string = postgresAdminLogin
output vaultName string = vault.name
output sqlHost string = databaseEngine == 'mssql' ? sqlServer!.properties.fullyQualifiedDomainName : ''
output sqlDatabaseName string = databaseEngine == 'mssql' ? sqlDatabaseName : ''
output identityClientId string = identity.properties.clientId
output identityPrincipalId string = identity.properties.principalId
