# Azure Container Apps

The application still runs locally with SQLite using the commands in the
[main README](../README.md). Azure is a separate environment with a separate
PostgreSQL database; deployment does not copy local SQLite data.

## What is provisioned

- An Azure Container Registry (Basic, administrator login disabled).
- A VNet-integrated Container Apps environment with Log Analytics.
- PostgreSQL Flexible Server 17, with a private delegated subnet and private DNS,
  32 GiB storage, and seven-day backups. There is no public database endpoint.
- A Key Vault containing the database password, stable Django session secret,
  and Entra client secret. Purge protection is enabled.
- A user-assigned managed identity with registry pull and Key Vault secret-read
  access. Neither registry credentials nor secret values are baked into images.
- The web app, with HTTPS ingress, one minimum replica, up to three replicas,
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
- Docker with Buildx and Linux containers. Build `linux/amd64` even when your
  development machine is Windows ARM64.
- An Azure subscription and permission to create the resources and assign roles
  (for example, Contributor plus User Access Administrator at resource-group
  scope). Have the required resource providers registered: `Microsoft.App`,
  `Microsoft.OperationalInsights`, `Microsoft.Network`,
  `Microsoft.DBforPostgreSQL`, `Microsoft.ContainerRegistry`, `Microsoft.KeyVault`,
  and `Microsoft.ManagedIdentity`. Your deployment identity must also be allowed
  to manage Key Vault secrets under your organization's policy.
- An existing Entra External ID external-tenant application, as described in the
  main README. Azure infrastructure provisioning does not create that application.
- Your organization-approved pip configuration file. The Docker build accepts it
  as a BuildKit secret, so private feed credentials do not enter image layers.
  The build runner must be able to reach that feed. Corporate TLS interception
  may require an organization-approved CA configuration in the build image.

The commands below use PowerShell 7 from the repository root. Azure CLI commands
must succeed before you continue to the next step. Do not deploy in complete mode.

## 1. Provision the infrastructure

Choose a supported Azure region for both Container Apps and PostgreSQL. The
lowercase prefix must start with a letter, contain only letters/digits/hyphens,
end in a letter/digit, and be 2-8 characters long. Avoid consecutive hyphens.
If changing the prefix, edit `infra.bicepparam` and use the same value below.

```powershell
az login
$rg = "pysign-rg"
$location = "eastus"
$prefix = "pysign"
az group create --name $rg --location $location

$env:PYSIGN_POSTGRES_PASSWORD = Read-Host -MaskInput "New PostgreSQL password (16+ characters)"
$env:PYSIGN_SECRET_KEY = .\.venv\Scripts\python.exe -c "import secrets; print(secrets.token_urlsafe(64))"
$env:PYSIGN_ENTRA_CLIENT_SECRET = Read-Host -MaskInput "Entra application client secret"

az deployment group create --name pysign-infra --resource-group $rg --parameters .\deploy\infra.bicepparam
$acr = az deployment group show --name pysign-infra --resource-group $rg --query properties.outputs.registryName.value -o tsv
$registry = az deployment group show --name pysign-infra --resource-group $rg --query properties.outputs.registryLoginServer.value -o tsv
```

`infra.bicepparam` reads secrets from environment variables rather than putting
their values in source or command-line arguments. Keep the same database password
and Django secret if redeploying infrastructure: do not generate replacements on
every release. Store them in your organization's approved secret system. Rotating
the Django secret signs out existing sessions and changes rate-limit keys.
Do not save compiled parameter files containing secrets.

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
$imageTag = "pysign:$tag"
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
$env:PYSIGN_ENTRA_TENANT_ID = "your-external-directory-uuid"
$env:PYSIGN_ENTRA_SUBDOMAIN = "your-tenant-subdomain"
$env:PYSIGN_ENTRA_CLIENT_ID = "your-application-client-id"
$parameters = @(
    "prefix=$prefix"
    "imageTag=$imageTag"
    "entraTenantId=$env:PYSIGN_ENTRA_TENANT_ID"
    "entraSubdomain=$env:PYSIGN_ENTRA_SUBDOMAIN"
    "entraClientId=$env:PYSIGN_ENTRA_CLIENT_ID"
)

az deployment group create --name pysign-jobs --resource-group $rg --template-file .\deploy\runtime.bicep --parameters @parameters deployWeb=false
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
az deployment group create --name pysign-runtime --resource-group $rg --template-file .\deploy\runtime.bicep --parameters @parameters deployWeb=true
$appUrl = az deployment group show --name pysign-runtime --resource-group $rg --query properties.outputs.appUrl.value -o tsv
$redirectUri = az deployment group show --name pysign-runtime --resource-group $rg --query properties.outputs.redirectUri.value -o tsv
$appUrl
$redirectUri
Invoke-RestMethod "$appUrl/health/live/"
Invoke-RestMethod "$appUrl/health/ready/"
```

Add the exact `$redirectUri` as a **Web** redirect URI in the Entra External ID
application and verify sign-in, pairing, playlist changes, and player polling.
Keep the localhost callback too if you use that registration locally.

Leave Container Apps built-in authentication disabled: PySign authenticates users
with Entra and devices with independent bearer tokens. A global interactive login
gate would block player API requests and probes.

The app and jobs share database-backed Django sessions, database state, and the
same Django secret. Login can start and finish on different replicas without
session affinity. The daily job runs `python manage.py cleanup_signage`.
An administrator can also start that job manually.

## HTTPS, probes, and proxy boundaries

`PYSIGN_AZURE_CONTAINER_APPS=1` trusts the scheme header overwritten by ACA and
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
`PYSIGN_CSRF_TRUSTED_ORIGINS` only when your actual origin setup requires it;
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

```powershell
$env:PYSIGN_DEBUG = "1"
Remove-Item Env:PYSIGN_AZURE_CONTAINER_APPS -ErrorAction SilentlyContinue
Remove-Item Env:PYSIGN_DATABASE_ENGINE -ErrorAction SilentlyContinue
python -m pip install --only-binary=cryptography -e '.[server]'
python manage.py migrate
python manage.py test signage.tests
python manage.py runserver
```

You do not need the `production` extra for this native Windows/SQLite workflow.
Optional PostgreSQL settings do nothing until you explicitly select that engine.
The player and its tests target Linux/Raspberry Pi and use `fcntl`, which is not
available on native Windows. Run the combined server/player suite on Linux.
