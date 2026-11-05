using './infra.bicep'

param prefix = 'pysign'
param postgresAdminPassword = readEnvironmentVariable('PYSIGN_POSTGRES_PASSWORD')
param djangoSecretKey = readEnvironmentVariable('PYSIGN_SECRET_KEY')
param entraClientSecret = readEnvironmentVariable('PYSIGN_ENTRA_CLIENT_SECRET')
