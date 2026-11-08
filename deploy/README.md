# Azure Container Apps

The application still runs locally with SQLite using the commands in the
[local setup guide](../docs/reference.md#server-local-setup). Azure is a separate environment with a separate
PostgreSQL database or a private schema in an existing Azure SQL database;
deployment does not copy local SQLite data.

For an existing installation, read the
[application rename migration notes](../docs/reference.md#updating-an-existing-installation-after-the-rename)
before deploying: the resource, database, image, and environment-variable defaults
have changed. These templates do not rename or migrate existing Azure resources.

## What is provisioned

- An Azure Container Registry (Basic, administrator login disabled), or reuse of
  an existing registry with pull access granted to PiSign's new managed identity.
- A VNet-integrated Container Apps environment with Log Analytics, or reuse of an
  existing environment without changing its networking, logging, or other apps.
- PostgreSQL Flexible Server 17, with a private delegated subnet and private DNS,
  32 GiB storage, and seven-day backups. There is no public database endpoint.
  Alternatively, Azure SQL mode uses an **existing database**, with a private
  endpoint and private DNS when creating a new environment. Reusing an environment
  requires existing SQL connectivity; no networking or DNS resources are changed.
  It does not create or reconfigure the SQL database.
- A Key Vault containing the database password, stable Django session secret,
  and Entra client secret. Purge protection is enabled.
- A user-assigned managed identity with registry pull and Key Vault secret-read
  access. Neither registry credentials nor secret values are baked into images.
- The web app, with HTTPS ingress, one replica by default,
  startup/liveness/readiness probes, and single-revision rollout.
- A manual migration job and a cleanup job scheduled for 03:00 UTC daily.

Resources incur Azure charges, including PostgreSQL when the app is idle.
The initial database is deliberately small and not highly available. Review
backup retention, capacity, availability, monitoring, and network ranges for
your production requirements. The VNet uses `10.42.0.0/16`; change it before
deployment if it overlaps networks you intend to connect.

The template initially uses the PostgreSQL administrator login for migrations
and application access. For a hardened deployment, create a database-scoped
application role, store its password in Key Vault, and separate runtime and
migration credentials. Managed identity is used for Azure resource access,
not PostgreSQL authentication or consumer sign-in.

## Prerequisites

- Azure CLI with current Bicep support (`az bicep upgrade`) and Container Apps
  commands (`az extension add --name containerapp --upgrade` if needed).
- For local image builds, Docker with Buildx and Linux containers. Build
  `linux/amd64` even when your development machine is Windows ARM64.
  Provisioning, Azure-hosted builds, and deployment of a prebuilt pipeline image
  do not require Docker.
- An Azure subscription and permission to create the resources and assign roles
  (for example, Contributor plus User Access Administrator at resource-group
  scope). Have the required resource providers registered: `Microsoft.App`,
  `Microsoft.OperationalInsights`, `Microsoft.Network`,
  `Microsoft.DBforPostgreSQL`, `Microsoft.ContainerRegistry`, `Microsoft.KeyVault`,
  and `Microsoft.ManagedIdentity`. Your deployment identity must also be allowed
  to manage Key Vault secrets under your organization's policy.
- An existing Entra External ID external-tenant application, as described in the
  main README. Azure infrastructure provisioning does not create that application.
- For Azure SQL, register `Microsoft.Sql` and have permission to read the existing
  logical server/database and, for a new environment, approve its new private
  endpoint. The server must be in the same Azure subscription as this deployment.
- On the image build runner, your organization-approved pip configuration file.
  The Docker build accepts it
  as a BuildKit secret, so private feed credentials do not enter image layers.
  The build runner must be able to reach that feed. Corporate TLS interception
  may require an organization-approved CA configuration in the build image.

The commands below use PowerShell 7 from the repository root. Azure CLI commands
must succeed before you continue to the next step. Do not deploy in complete mode.

## Automated deployment with Azure Developer CLI

Use **Azure Developer CLI (`azd`) 1.37.2 or newer** to run the deployment sequence
instead of entering the commands in sections 1-4 individually. The Azure CLI
(`az`) prerequisite still applies; Docker is needed only for local builds. Run from the repository root
in PowerShell 7:

```powershell
azd auth login
az login
azd env new pisign
azd env set AZURE_SUBSCRIPTION_ID "your-subscription-uuid"
azd env set AZURE_LOCATION centralus
azd env set PISIGN_ENTRA_TENANT_ID "your-external-directory-uuid"
azd env set PISIGN_ENTRA_SUBDOMAIN "your-tenant-subdomain"
azd env set PISIGN_ENTRA_CLIENT_ID "your-application-client-id"
azd env set PISIGN_PIP_CONFIG "C:\path\to\your\approved-pip.ini"

$env:PISIGN_POSTGRES_PASSWORD = Read-Host -MaskInput "New PostgreSQL password (16+ characters)"
$env:PISIGN_SECRET_KEY = .\.venv\Scripts\python.exe -c "import secrets; print(secrets.token_urlsafe(64))"
$env:PISIGN_ENTRA_CLIENT_SECRET = Read-Host -MaskInput "Entra application client secret"

azd up
```

Replace the placeholders before running. The secret-generation command assumes
the local Python virtual environment from the main README exists. Store the
initial database password and Django secret securely and reuse those same values
for subsequent provisioning; do not generate a new Django secret on each release.
Set secrets in the process environment, not with `azd env set`, so you do not
intentionally persist them in the environment's `.env` file. Protect local `azd`
state, which can contain sensitive deployment parameters. `.azure/` is excluded
from Git and the Docker build context.

`azd up` provisions infrastructure in Central US, builds and pushes a unique
`linux/amd64` image using the approved pip configuration as a BuildKit secret,
deploys the migration job, and waits up to 15 minutes for it to succeed. Only then
does it deploy the web app and scheduled cleanup job and check both health
endpoints. A failed command, failed migration, or timeout stops the sequence.
Inspect the migration execution before retrying after a timeout: it may still be
running. Do not run deployments concurrently against the same environment.

The subscription-scoped `main.bicep` wrapper reuses `infra.bicep` and creates
`rg-pisign` for the environment named `pisign`, using the `pisign` resource prefix.
For an existing resource group, set `AZURE_RESOURCE_GROUP` with `azd env set`
before provisioning, retain its original region and secrets, and ensure its
resource prefix is `pisign`. This does not migrate resources or local SQLite data.
Use a new resource group for a move to Central US.

The configuration deliberately has no native `azd` services: a project-level
PowerShell deployment hook owns image building and runtime deployment, preserving
the migration-before-rollout behavior of the manual commands. `azd provision`
only provisions infrastructure and does not require Docker or pip configuration;
`azd deploy` runs the build/migrate/rollout steps
against the active environment's existing infrastructure:

```powershell
azd deploy
```

### Azure-hosted image builds (no local Docker or CI pipeline)

To build in your Azure Container Registry while still deploying from this
project with `azd deploy`, set:

```powershell
azd env set PISIGN_IMAGE_SOURCE remote
azd env set PISIGN_PIP_CONFIG "C:\path\outside\this\repository\approved-pip.ini"
azd deploy
```

Complete infrastructure provisioning and, for shared SQL, the one-time user/schema
setup before the first deployment. This mode works with either a provisioned or
reused registry. It requires Azure CLI, `tar` (included in Windows), and permission
to upload build source and schedule/read ACR runs. Non-Windows hosts also need
`chmod` to protect temporary files. The registry must support ACR Tasks and its
build agents must be able to reach the approved pip feed and base-image registries.
Remote builds incur ACR Tasks charges; they do not require a separate CI pipeline,
local Docker, or a persistent task resource.

The hook uploads only the Dockerfile's application inputs, excluding local
environment/database/cache files. Keep the pip configuration outside the
repository. It is sent separately as an ACR secret value, using an access-restricted
temporary request file rather than credentials in command-line arguments.
The task mounts it into BuildKit as the same required `pip_config` secret used
by local builds; it is not copied into image layers. Temporary request/archive
files are removed on success or failure. Do not enable Azure CLI debug/HTTP
request logging when deploying with secret values.

The hook waits for the remote build to succeed, resolves its unique image tag
to an immutable digest, then runs migrations before rolling out the web app.
Build failures, unknown statuses, and timeouts stop deployment before any
runtime changes. A timed-out build may still be queued/running; inspect the
printed run ID before retrying. Read build logs with
`az acr task logs --registry <registry-name> --run-id <run-id>`.
To return to local Docker builds, set `PISIGN_IMAGE_SOURCE` to `local`.

For later releases, `azd deploy` reuses the Key Vault secrets; the three
provisioning secrets do not need to be set locally. Use `azd up` or `azd provision`
after infrastructure changes, with the original secrets supplied again.

**One manual Entra step remains:** after the first deployment, register the exact
Web redirect URI printed by the hook in your external-tenant application.
Infrastructure deployment does not modify that application registration.
Then verify sign-in, pairing, playlist changes, and player polling. Keep the
localhost callback if you also use the registration locally.

### Shared existing Azure SQL database

This mode uses the **existing database you explicitly name**, preserving its
pricing tier, free-limit settings, capacity, global collation, and existing
application tables. It does not create a database or change the logical server's
administrator, Entra administrator, firewall, or public-network settings. It adds
a private endpoint for PiSign's VNet only when creating a new Container Apps
environment. The server's resource group can differ from
PiSign's resource group, in the same subscription.

All PiSign tables live in schema **`pisign`**, including `auth_user`,
`django_sessions`, `django_content_type`, and `django_migrations`. Standard
Django table names do not collide with another application's `dbo` tables.
The SQL backend inspects only the PiSign schema and rejects connections whose
database user/default schema are not `pisign-app`/`pisign`. Its string columns use
case-sensitive collation without changing the database's collation.

Use a fresh `azd` environment when changing from PostgreSQL; this is not an
in-place data migration. Complete the authentication, subscription, Entra, and
pip-configuration setup above, but **do not set a PostgreSQL password**. Instead:

```powershell
azd env set PISIGN_DATABASE_ENGINE mssql
azd env set PISIGN_SQL_SERVER_NAME "your-existing-logical-server"
azd env set PISIGN_SQL_SERVER_RESOURCE_GROUP "your-existing-server-resource-group"
azd env set PISIGN_SQL_NAME "your-existing-application-database"

$env:PISIGN_SECRET_KEY = .\.venv\Scripts\python.exe -c "import secrets; print(secrets.token_urlsafe(64))"
$env:PISIGN_ENTRA_CLIENT_SECRET = Read-Host -MaskInput "Entra application client secret"
azd provision
```

Use the server's short name, not a connection string or hostname. The hook
queries the server/database read-only and stops if the selected database does
not exist or the logical server has no Entra administrator configured for
managed-identity authentication. It does not configure or replace that administrator.
Database names must start with a letter and contain only letters,
digits, underscores, and hyphens; system databases are rejected.

The app runs in Central US by default; the existing SQL server can be in another
region. The private endpoint, Container Apps, registry, and logging incur separate
charges. Both applications share SQL compute/storage capacity, and PiSign's
polling can increase resource consumption or prevent serverless idle auto-pause.
This configuration does not promise free operation or performance isolation.

#### One-time schema-scoped managed identity setup

The existing logical server must already have a suitable **Entra administrator**.
The deployment does not replace it. As that administrator, use your existing
approved SQL connectivity and `sqlcmd` with Entra authentication to run:

```powershell
$sqlServer = azd env get-value PISIGN_SQL_SERVER_NAME
$sqlDatabase = azd env get-value PISIGN_SQL_NAME
$identityClientId = azd env get-value PISIGN_SQL_CLIENT_ID
sqlcmd -S "tcp:$sqlServer.database.windows.net,1433" -d $sqlDatabase -G -b `
    -i .\deploy\sql-user.sql `
    -v "DatabaseName=$sqlDatabase" "ManagedIdentityClientId=$identityClientId"
```

The script creates a new `pisign-app` user and `pisign` schema, mapped to the
managed identity's **application/client ID**, not its principal/object ID. It refuses to
adopt a pre-existing schema without that user or a user/schema owned by another
identity. It is safe to rerun for the same isolated identity.

SQL query timeouts default to five seconds. The migration job sets
`PISIGN_SQL_QUERY_TIMEOUT=60` to allow slower schema metadata queries on shared
SQL capacity without increasing the web app's timeout. Overrides must be
positive integer seconds.

The identity gets `CREATE TABLE` at database level (required by SQL Server),
with table creation, read/write, DDL, references, and metadata permissions
restricted to its own schema. It gets explicit denials on other existing
application schemas, including `dbo`, and no broad database roles. Existing
database-wide role memberships or unrelated direct grants cause setup to stop.
Do not subsequently add broad roles, permissions, or public grants that bypass
this isolation. If adding new schemas to the shared database later, review their
public grants and the PiSign user's restrictions.

The same identity runs migrations and the app, so it can change objects in its
own schema. For additional hardening, separate migration/runtime identities.
No SQL administrator password or database password is supplied to the app,
deployment hook, or Key Vault. Do not grant it access to other databases.
If `sqlcmd` cannot reach the server, use an already approved administrator
connection; do not open the database to all Azure services or change the existing
server's firewall just to bypass this step.

Now deploy the image, run migrations, and roll out the app:

```powershell
azd deploy
```

Subsequent releases use `azd deploy`; infrastructure updates use `azd provision`
with the original Django/Entra secrets, or `azd up` after the user has been created.
Register the printed Entra callback URI as described above.

### Existing Container Apps environment and registry

Python and .NET apps can share a Container Apps environment as separate apps.
This mode currently supports the existing Azure SQL database configuration above.
It creates PiSign's identity, Key Vault, app, and jobs in a **separate resource
group**; it does not redeploy another app, change the shared environment, or
create a new VNet, private endpoint, DNS zone, Log Analytics workspace, or registry.
An additional `AcrPull` assignment gives only the PiSign identity pull access to
the existing registry. The registry must use classic RBAC permissions; the hook
rejects ABAC mode rather than changing a shared registry's permissions mode.
Your deployment account needs read access to the shared resources and permission
to assign that role on the registry in its resource group.

The environment must already have approved network access to the existing SQL
server, including any required private DNS or SQL firewall rules. This deployment
does not alter those settings. A failed migration, including a connection or image
pull failure, stops web rollout. Consumption-only environments and environments
with a `Consumption` workload profile are supported; dedicated-only environments
are rejected without modifying their profiles.

After creating a fresh `azd` environment and setting the subscription, Entra, and
SQL inputs above, configure the existing resources:

```powershell
azd env set PISIGN_EXISTING_ENVIRONMENT_NAME "your-existing-environment"
azd env set PISIGN_EXISTING_ENVIRONMENT_RESOURCE_GROUP "its-resource-group"
azd env set PISIGN_EXISTING_REGISTRY_NAME "your-existing-registry"
azd env set PISIGN_EXISTING_REGISTRY_RESOURCE_GROUP "its-resource-group"
azd env set PISIGN_IMAGE_SOURCE remote
azd env set PISIGN_PIP_CONFIG "C:\path\outside\this\repository\approved-pip.ini"

$location = az containerapp env show --name "your-existing-environment" `
    --resource-group "its-resource-group" --subscription "your-subscription-uuid" `
    --query location --output tsv
if ($LASTEXITCODE -ne 0) { throw "Cannot read the existing environment." }
azd env set AZURE_LOCATION $location

# Supply the original Django/Entra secrets as described above.
azd provision
```

The location must match the existing environment; it is not necessarily Central
US, and setting the location does not move that environment. Do not set
`AZURE_RESOURCE_GROUP` to any shared environment, registry, or SQL resource group.
The default `rg-pisign` keeps PiSign's resource-group lifecycle separate.
If that group already contains an External ID tenant or other independently
managed resources, set `AZURE_RESOURCE_GROUP` to a fresh server-only group
(for example, `rg-pisign-server`) before provisioning. Do not bring those
resources into the resource-group lifecycle of `azd down`.
The hook rejects an existing app with the same name in the environment unless
it is marked as PiSign-managed and resides in PiSign's resource group. It also
rejects unmanaged jobs or jobs assigned to a different environment.

Run the one-time schema-scoped SQL identity setup above after provisioning.
Then run `azd deploy` to build PiSign in the reused registry and deploy it.
No CI pipeline or local Docker installation is required. See the Azure-hosted
build requirements and secret handling above. Select `local` instead of `remote`
if you prefer to build with local Docker.

#### Optional prebuilt image from a CI pipeline

If you already have a pipeline, set `PISIGN_IMAGE_SOURCE` to `pipeline`.
Have it build **this repository's Dockerfile**, not the
other application's .NET image, and push it to the selected registry. Its build
step needs Buildx, `linux/amd64`, and the approved pip configuration secret:

```powershell
docker buildx build --platform linux/amd64 `
    --secret "id=pip_config,src=$env:PISIGN_PIP_CONFIG" `
    --tag "your-existing-registry.azurecr.io/pisign:unique-release-tag" `
    --push .
```

Configure registry authentication in the pipeline using your existing approved
service connection or federated identity. Do not commit feed credentials, Azure
tokens, or app secrets. Building in the pipeline requires no Docker or WSL
installation on your local deployment machine.

Once the pipeline has successfully pushed the image:

```powershell
azd env set PISIGN_IMAGE_SOURCE pipeline
azd env set PISIGN_IMAGE_TAG "pisign:unique-release-tag"
azd deploy
```

`PISIGN_IMAGE_TAG` must name an existing `repository:tag` or
`repository@sha256:digest`, without the registry hostname. The hook resolves it
to an immutable digest and uses exactly that image for migrations, web, and
cleanup. No local image build, pip configuration, or Docker login is performed.
Update the image reference after each successful pipeline build. Omitting
`PISIGN_IMAGE_SOURCE`, or setting it to `local`, retains the local-build workflow.
Unset the existing-resource inputs to retain the original new-environment path;
switching an existing installation between environments is not a data migration.

SQL connections use Microsoft ODBC Driver 18, certificate-validated TLS, and
Azure Identity to obtain a fresh access token for each new connection. The image
includes the SQL backend/driver while PostgreSQL and local SQLite remain usable.
Local pooling is disabled for SQL mode. Authentication or connection failures
are not treated as successful health checks.

**Shared-database safety:** back up the database before first setup/migration.
Do not use `azd down`, delete the shared database/resource group, or use Django
flush/test-database creation with an administrator account as a cleanup shortcut.
Remove only explicitly identified PiSign resources, its schema, and its user
after reviewing dependencies and backups. Existing application tables are not
renamed, migrated, or granted to PiSign.

The manual steps below continue to default to PostgreSQL. For manual Azure SQL
deployment, first run the same existing-database preflight with
`.\deploy\azd.ps1 -Phase provision` in a configured environment, then pass
`databaseEngine=mssql`, `sqlServerName`, `sqlServerResourceGroup`,
and `sqlDatabaseName` to `infra.bicep`. Complete the user setup
above before deploying jobs. Pass `databaseEngine=mssql`,
`sqlHost=<server>.database.windows.net`, and `sqlDatabaseName` to both runtime
deployments. Do not supply PostgreSQL credentials in SQL mode.

## Manual deployment

The following sections are an alternative to `azd up`, not additional steps
required after it.

## 1. Provision the infrastructure

The commands below use **Central US** (`centralus`) for both Container Apps and
PostgreSQL. Both Bicep templates default to the resource group's location, so
the infrastructure, web app, and jobs use the same region without overrides.
To choose another supported region, change `$location` before creating the
resource group. The lowercase prefix must start with a letter, contain only letters/digits/hyphens,
end in a letter/digit, and be 2-8 characters long. Avoid consecutive hyphens.
If changing the prefix, edit `infra.bicepparam` and use the same value below.

```powershell
az login
$rg = "pisign-rg"
$location = "centralus"
$prefix = "pisign"
az group create --name $rg --location $location

$env:PISIGN_POSTGRES_PASSWORD = Read-Host -MaskInput "New PostgreSQL password (16+ characters)"
$env:PISIGN_SECRET_KEY = .\.venv\Scripts\python.exe -c "import secrets; print(secrets.token_urlsafe(64))"
$env:PISIGN_ENTRA_CLIENT_SECRET = Read-Host -MaskInput "Entra application client secret"

az deployment group create --name pisign-infra --resource-group $rg --parameters .\deploy\infra.bicepparam
$acr = az deployment group show --name pisign-infra --resource-group $rg --query properties.outputs.registryName.value -o tsv
$registry = az deployment group show --name pisign-infra --resource-group $rg --query properties.outputs.registryLoginServer.value -o tsv
```

`infra.bicepparam` reads secrets from environment variables rather than putting
their values in source or command-line arguments. Keep the same database password
and Django secret if redeploying infrastructure: do not generate replacements on
every release. Store them in your organization's approved secret system. Rotating
the Django secret signs out existing sessions and changes rate-limit keys.
Do not save compiled parameter files containing secrets.

For an existing deployment in another region, use a new resource group in
Central US and plan data migration separately. Changing `$location` does not
move an existing resource group or its resources.

New role assignments can take time to propagate. If an initial runtime deployment
reports Key Vault or registry authorization failures, allow propagation and retry;
do not enable anonymous access or registry administrator credentials.

## 2. Build and push an immutable image

Use the approved pip configuration for your company feed. For example, a file with
`[global]` and `index-url = https://packagefeedproxy.microsoft.io/pypi/simple/`.
Do not place feed credentials in a Docker build argument or commit that file.

```powershell
$pipConfig = "C:\path\to\your\approved-pip.ini"
$tag = Get-Date -AsUTC -Format "yyyyMMddHHmmss"
$imageTag = "pisign:$tag"
az acr login --name $acr
docker buildx build --platform linux/amd64 --secret "id=pip_config,src=$pipConfig" --tag "$registry/$imageTag" --push .
```

The image includes Gunicorn, the PostgreSQL driver, WhiteNoise, and collected,
hashed static assets. It runs as a non-root user and binds `0.0.0.0:8000`.
No migration or database connection runs during image creation or web startup.
The default container database engine is PostgreSQL; missing configuration fails
explicitly. Native local development still defaults to SQLite.

## 3. Deploy the migration job, then run it once

Set the non-secret Entra values. The client secret is already in Key Vault.

```powershell
$env:PISIGN_ENTRA_TENANT_ID = "your-external-directory-uuid"
$env:PISIGN_ENTRA_SUBDOMAIN = "your-tenant-subdomain"
$env:PISIGN_ENTRA_CLIENT_ID = "your-application-client-id"
$parameters = @(
    "prefix=$prefix"
    "imageTag=$imageTag"
    "entraTenantId=$env:PISIGN_ENTRA_TENANT_ID"
    "entraSubdomain=$env:PISIGN_ENTRA_SUBDOMAIN"
    "entraClientId=$env:PISIGN_ENTRA_CLIENT_ID"
)

az deployment group create --name pisign-jobs --resource-group $rg --template-file .\deploy\runtime.bicep --parameters @parameters deployWeb=false
$execution = az containerapp job start --name "$prefix-migrate" --resource-group $rg --query name -o tsv
az containerapp job execution show --name "$prefix-migrate" --resource-group $rg --job-execution-name $execution --query properties.status -o tsv
```

Repeat the last status command until the execution reports **Succeeded**.
Starting a job successfully is not the same as completing migrations. If it
reports **Failed**, inspect its execution logs and fix the error before continuing.
Never run concurrent migration executions. Jobs have no automatic replica retries.

At this stage the first deployment has no public app or scheduled cleanup job.
On later deployments, the previous web revision and cleanup job remain untouched
because ARM deployments are incremental.

## 4. Deploy the web app and scheduled cleanup

Only continue after the migration execution succeeds:

```powershell
az deployment group create --name pisign-runtime --resource-group $rg --template-file .\deploy\runtime.bicep --parameters @parameters deployWeb=true
$appUrl = az deployment group show --name pisign-runtime --resource-group $rg --query properties.outputs.appUrl.value -o tsv
$redirectUri = az deployment group show --name pisign-runtime --resource-group $rg --query properties.outputs.redirectUri.value -o tsv
$appUrl
$redirectUri
Invoke-RestMethod "$appUrl/health/live/"
Invoke-RestMethod "$appUrl/health/ready/"
```

Add the exact `$redirectUri` as a **Web** redirect URI in the Entra External ID
application and verify sign-in, pairing, playlist changes, and player polling.
Keep the localhost callback too if you use that registration locally.

Leave Container Apps built-in authentication disabled: PiSign authenticates users
with Entra and devices with independent bearer tokens. A global interactive login
gate would block player API requests and probes.

The app and jobs share database-backed Django sessions, database state, and the
same Django secret. Login can start and finish on different replicas without
session affinity. The daily job runs `python manage.py cleanup_signage`.
An administrator can also start that job manually.

## HTTPS, probes, and proxy boundaries

`PISIGN_AZURE_CONTAINER_APPS=1` trusts the scheme header overwritten by ACA and
uses only the rightmost client address appended by ACA to `X-Forwarded-For`.
Earlier addresses are untrusted. Missing/invalid ACA addresses produce a logged
400 response instead of silently grouping clients under a proxy IP. Do not enable
this mode on a directly exposed server or bypass ACA ingress. If adding another
proxy such as Front Door, the rightmost address may identify that proxy, not the
browser; design the additional trust boundary explicitly.

`/health/live/` has no database or Entra dependency. `/health/ready/` executes
`SELECT 1` with a five-second PostgreSQL connection timeout and returns 503 when
the database is unavailable. These endpoints intentionally accept direct probe
requests without public hostnames, HTTPS redirects, session lookup, or proxy
headers. They disclose only status, not configuration or database errors.

The default Azure hostname is configured in `ALLOWED_HOSTS` and the Entra callback.
For a custom domain, bind its DNS/certificate in ACA, then update both settings
in `runtime.bicep` and register the exact new callback with Entra. Add
`PISIGN_CSRF_TRUSTED_ORIGINS` only when your actual origin setup requires it;
normal same-origin traffic does not need it.

Gunicorn logs to stdout/stderr. Its access log excludes query strings and
authorization headers, so callback codes are not logged by this configuration.
Apply the same redaction to any additional proxies or diagnostic services.

`python manage.py check --deploy` may report `security.W005` and `security.W021`:
HSTS is enabled, but subdomain coverage and preload are deliberately not opted
into. Enable those only after reviewing a custom domain and every affected
subdomain; do not make a blanket preload policy for Azure's shared domain.

## Subsequent releases and rollback

Build a new, unique image tag; redeploy the migration job; run it and wait for
success; then deploy the web app and cleanup job with the same tag. Do not rotate
infrastructure secrets as part of an ordinary release.

Use backward-compatible database migrations: the old revision can still run
while migrations and readiness checks execute. Roll back by redeploying a known
image tag only if it is compatible with the current database schema. Never
automatically reverse database migrations during a replica restart.

Azure PostgreSQL is private, so local database tools cannot connect directly.
Use approved VNet connectivity or a job in the environment for database work.
Do not open the database to all Azure services or the internet to bypass this.

## Local verification

Validate the PowerShell deployment hook without Azure credentials or Docker:

```powershell
pwsh -NoProfile -File .\deploy\tests\azd.Tests.ps1
```

These mocked tests cover provisioning checks, first deployment, redeployment,
immutable image tags, migration-before-rollout ordering, active migrations,
command failures, unsuccessful migrations, timeouts, health-check failures, SQL
existing-database checks and SQL runtime parameters.
They do not replace an actual Azure deployment and hardware acceptance.

Run SQL connection/configuration tests locally without contacting Azure:

```powershell
.\.venv\Scripts\python.exe -m pip install --only-binary=cryptography,pyodbc -e '.[server,azure-sql]'
$env:PISIGN_DEBUG = "1"
$env:PISIGN_DATABASE_ENGINE = "sqlite"
.\.venv\Scripts\python.exe manage.py test signage.tests.test_deployment
```

Before relying on SQL mode, run migrations and exercise sign-in, pairing multiple
devices, playlists, rate limits, revocation, and health probes inside the PiSign
schema. Verify its user cannot read/change other schemas and that the other
application still works. The local SQLite suite and mocked tests do not prove
live SQL Server compatibility. Never run Django's test-database creation against
the shared database using a broadly permissioned administrator account.

```powershell
$env:PISIGN_DEBUG = "1"
Remove-Item Env:PISIGN_AZURE_CONTAINER_APPS -ErrorAction SilentlyContinue
Remove-Item Env:PISIGN_DATABASE_ENGINE -ErrorAction SilentlyContinue
python -m pip install --only-binary=cryptography -e '.[server]'
python manage.py migrate
python manage.py test signage.tests
python manage.py runserver
```

You do not need the `production` extra for this native Windows/SQLite workflow.
Optional PostgreSQL settings do nothing until you explicitly select that engine.
The player and its tests target Linux/Raspberry Pi and use `fcntl`, which is not
available on native Windows. Run the combined server/player suite on Linux.
