targetScope = 'resourceGroup'

@minLength(2)
@maxLength(8)
param prefix string = 'pisign'
param location string = resourceGroup().location

@description('Immutable repository:tag already pushed to the registry created by infra.bicep.')
@minLength(1)
param imageTag string
param entraTenantId string
param entraSubdomain string
param entraClientId string

@description('Leave false until the migration job succeeds; then deploy again with true.')
param deployWeb bool = false

@minValue(1)
param minReplicas int = 1
@minValue(1)
param maxReplicas int = 3

var suffix = uniqueString(resourceGroup().id, prefix)
var registryName = '${replace(prefix, '-', '')}${suffix}'
var vaultName = '${prefix}-${suffix}'

resource environment 'Microsoft.App/managedEnvironments@2025-01-01' existing = {
  name: '${prefix}-env'
}
resource identity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' existing = {
  name: '${prefix}-identity'
}
resource registry 'Microsoft.ContainerRegistry/registries@2023-07-01' existing = {
  name: registryName
}
resource vault 'Microsoft.KeyVault/vaults@2023-07-01' existing = {
  name: vaultName
}
resource postgres 'Microsoft.DBforPostgreSQL/flexibleServers@2024-08-01' existing = {
  name: '${prefix}-${suffix}-pg'
}

var hostname = '${prefix}.${environment.properties.defaultDomain}'
var image = '${registry.properties.loginServer}/${imageTag}'
var containerIdentity = {
  type: 'UserAssigned'
  userAssignedIdentities: { '${identity.id}': {} }
}
var registryCredentials = [
  {
    server: registry.properties.loginServer
    identity: identity.id
  }
]
var secrets = [
  {
    name: 'postgres-password'
    keyVaultUrl: '${vault.properties.vaultUri}secrets/postgres-password'
    identity: identity.id
  }
  {
    name: 'django-secret-key'
    keyVaultUrl: '${vault.properties.vaultUri}secrets/django-secret-key'
    identity: identity.id
  }
  {
    name: 'entra-client-secret'
    keyVaultUrl: '${vault.properties.vaultUri}secrets/entra-client-secret'
    identity: identity.id
  }
]
var env = [
  { name: 'PISIGN_DEBUG', value: '0' }
  { name: 'PISIGN_AZURE_CONTAINER_APPS', value: '1' }
  { name: 'PISIGN_ALLOWED_HOSTS', value: hostname }
  { name: 'PISIGN_SECRET_KEY', secretRef: 'django-secret-key' }
  { name: 'PISIGN_DATABASE_ENGINE', value: 'postgresql' }
  { name: 'PISIGN_POSTGRES_NAME', value: 'pisign' }
  { name: 'PISIGN_POSTGRES_HOST', value: postgres.properties.fullyQualifiedDomainName }
  { name: 'PISIGN_POSTGRES_USER', value: postgres.properties.administratorLogin }
  { name: 'PISIGN_POSTGRES_PASSWORD', secretRef: 'postgres-password' }
  { name: 'PISIGN_POSTGRES_SSLMODE', value: 'verify-full' }
  { name: 'PISIGN_POSTGRES_SSLROOTCERT', value: '/etc/ssl/certs/ca-certificates.crt' }
  { name: 'PISIGN_ENTRA_TENANT_ID', value: entraTenantId }
  { name: 'PISIGN_ENTRA_SUBDOMAIN', value: entraSubdomain }
  { name: 'PISIGN_ENTRA_CLIENT_ID', value: entraClientId }
  { name: 'PISIGN_ENTRA_CLIENT_SECRET', secretRef: 'entra-client-secret' }
  { name: 'PISIGN_ENTRA_REDIRECT_URI', value: 'https://${hostname}/accounts/callback/' }
]

var jobDefinitions = [
  {
    name: '${prefix}-migrate'
    args: ['manage.py', 'migrate', '--noinput']
    triggerType: 'Manual'
  }
  {
    name: '${prefix}-cleanup'
    args: ['manage.py', 'cleanup_signage']
    triggerType: 'Schedule'
  }
]

resource jobs 'Microsoft.App/jobs@2025-01-01' = [for job in jobDefinitions: if (deployWeb || job.triggerType == 'Manual') {
  name: job.name
  location: location
  identity: containerIdentity
  properties: {
    environmentId: environment.id
    workloadProfileName: 'Consumption'
    configuration: {
      triggerType: job.triggerType
      replicaTimeout: 600
      replicaRetryLimit: 0
      manualTriggerConfig: job.triggerType == 'Manual' ? {
        parallelism: 1
        replicaCompletionCount: 1
      } : null
      scheduleTriggerConfig: job.triggerType == 'Schedule' ? {
        cronExpression: '0 3 * * *'
        parallelism: 1
        replicaCompletionCount: 1
      } : null
      registries: registryCredentials
      secrets: secrets
    }
    template: {
      containers: [
        {
          name: 'pisign'
          image: image
          command: ['python']
          args: job.args
          env: env
          resources: { cpu: json('0.5'), memory: '1Gi' }
        }
      ]
    }
  }
}]

resource app 'Microsoft.App/containerApps@2025-01-01' = if (deployWeb) {
  name: prefix
  location: location
  identity: containerIdentity
  properties: {
    managedEnvironmentId: environment.id
    workloadProfileName: 'Consumption'
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        external: true
        targetPort: 8000
        transport: 'http'
        allowInsecure: false
      }
      registries: registryCredentials
      secrets: secrets
    }
    template: {
      containers: [
        {
          name: 'pisign'
          image: image
          env: env
          resources: { cpu: json('0.5'), memory: '1Gi' }
          probes: [
            {
              type: 'Startup'
              httpGet: { path: '/health/live/', port: 8000, scheme: 'HTTP' }
              periodSeconds: 5
              timeoutSeconds: 2
              failureThreshold: 30
            }
            {
              type: 'Liveness'
              httpGet: { path: '/health/live/', port: 8000, scheme: 'HTTP' }
              periodSeconds: 30
              timeoutSeconds: 2
              failureThreshold: 3
            }
            {
              type: 'Readiness'
              httpGet: { path: '/health/ready/', port: 8000, scheme: 'HTTP' }
              periodSeconds: 10
              timeoutSeconds: 6
              failureThreshold: 3
            }
          ]
        }
      ]
      scale: {
        minReplicas: minReplicas
        maxReplicas: maxReplicas
        rules: [
          {
            name: 'http'
            http: { metadata: { concurrentRequests: '50' } }
          }
        ]
      }
    }
  }
}

output appUrl string = 'https://${hostname}'
output redirectUri string = 'https://${hostname}/accounts/callback/'
output migrationJob string = '${prefix}-migrate'
output cleanupJob string = '${prefix}-cleanup'
