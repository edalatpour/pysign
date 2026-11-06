targetScope = 'subscription'

param environmentName string
param location string = 'centralus'
param resourceGroupName string = ''

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

var prefix = 'pisign'

resource group 'Microsoft.Resources/resourceGroups@2024-03-01' = {
  name: empty(resourceGroupName) ? 'rg-${environmentName}' : resourceGroupName
  location: location
  tags: {
    'azd-env-name': environmentName
  }
}

module infrastructure './infra.bicep' = {
  scope: group
  params: {
    prefix: prefix
    location: location
    databaseEngine: databaseEngine
    sqlServerName: sqlServerName
    sqlServerResourceGroup: sqlServerResourceGroup
    sqlDatabaseName: sqlDatabaseName
    existingEnvironmentName: existingEnvironmentName
    existingEnvironmentResourceGroup: existingEnvironmentResourceGroup
    existingRegistryName: existingRegistryName
    existingRegistryResourceGroup: existingRegistryResourceGroup
    postgresAdminPassword: postgresAdminPassword
    djangoSecretKey: djangoSecretKey
    entraClientSecret: entraClientSecret
  }
}

output AZURE_RESOURCE_GROUP string = group.name
output AZURE_LOCATION string = location
output PISIGN_PREFIX string = prefix
output PISIGN_REGISTRY_NAME string = infrastructure.outputs.registryName
output PISIGN_REGISTRY_LOGIN_SERVER string = infrastructure.outputs.registryLoginServer
output PISIGN_REGISTRY_RESOURCE_GROUP string = infrastructure.outputs.registryResourceGroup
output PISIGN_CONTAINER_APPS_ENVIRONMENT_NAME string = infrastructure.outputs.environmentName
output PISIGN_CONTAINER_APPS_ENVIRONMENT_RESOURCE_GROUP string = infrastructure.outputs.environmentResourceGroup
output PISIGN_WORKLOAD_PROFILE_NAME string = infrastructure.outputs.workloadProfileName
output PISIGN_DATABASE_ENGINE string = databaseEngine
output PISIGN_SQL_HOST string = infrastructure.outputs.sqlHost
output PISIGN_SQL_NAME string = infrastructure.outputs.sqlDatabaseName
output PISIGN_SQL_CLIENT_ID string = infrastructure.outputs.identityClientId
output PISIGN_SQL_PRINCIPAL_ID string = infrastructure.outputs.identityPrincipalId
output PISIGN_SQL_SERVER_NAME string = sqlServerName
output PISIGN_SQL_SERVER_RESOURCE_GROUP string = sqlServerResourceGroup
