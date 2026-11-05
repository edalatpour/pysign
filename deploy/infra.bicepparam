using './infra.bicep'

param prefix = 'pisign'
param postgresAdminPassword = readEnvironmentVariable('PISIGN_POSTGRES_PASSWORD')
param djangoSecretKey = readEnvironmentVariable('PISIGN_SECRET_KEY')
param entraClientSecret = readEnvironmentVariable('PISIGN_ENTRA_CLIENT_SECRET')
