param(
    [Parameter(Mandatory)]
    [ValidateSet("provision", "deploy")]
    [string] $Phase
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Get-RequiredEnvironmentValue {
    param([string] $Name)
    $value = [Environment]::GetEnvironmentVariable($Name)
    if ([string]::IsNullOrWhiteSpace($value)) {
        throw "Set $Name before running azd $Phase. See deploy\README.md."
    }
    return $value
}

function Invoke-CheckedCommand {
    param(
        [string] $Command,
        [string[]] $Arguments
    )
    $output = & $Command @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$Command failed with exit code $LASTEXITCODE. Deployment stopped."
    }
    return $output
}

function Invoke-Azure {
    param([string[]] $Arguments)
    Invoke-CheckedCommand az ($Arguments + @("--subscription", $subscription))
}

function Invoke-RemoteBuild {
    param([string] $Image)
    $registryId = (Invoke-Azure @(
        "acr", "show", "--name", $acr, "--query", "id", "--output", "tsv"
    ) | Out-String).Trim()
    if ($registryId -notmatch "^/subscriptions/$([regex]::Escape($subscription))/resourceGroups/[^/]+/providers/Microsoft.ContainerRegistry/registries/$([regex]::Escape($acr))$") {
        throw "Azure did not return the selected registry's resource ID."
    }
    $manager = (Invoke-CheckedCommand az @("cloud", "show", "--query", "endpoints.resourceManager", "--output", "tsv") | Out-String).TrimEnd("/", "`r", "`n")
    if ($manager -notmatch '^https://[^/]+$') {
        throw "Azure did not return a valid HTTPS resource-manager endpoint."
    }
    $endpoint = "$manager$registryId"
    $temporary = Join-Path ([IO.Path]::GetTempPath()) ("pisign-build-" + [guid]::NewGuid().ToString("N"))
    $archive = Join-Path $temporary "source.tar.gz"
    $requestFile = Join-Path $temporary "request.json"
    New-Item -ItemType Directory -Path $temporary | Out-Null
    try {
        if ($IsWindows) {
            $acl = Get-Acl -LiteralPath $temporary
            $acl.SetAccessRuleProtection($true, $false)
            $owner = [Security.Principal.WindowsIdentity]::GetCurrent().User
            $rule = [Security.AccessControl.FileSystemAccessRule]::new(
                $owner, "FullControl", "ContainerInherit, ObjectInherit", "None", "Allow"
            )
            $acl.AddAccessRule($rule)
            Set-Acl -LiteralPath $temporary -AclObject $acl
        } else {
            Invoke-CheckedCommand chmod @("700", $temporary) | Out-Null
        }
        # Only upload Dockerfile inputs, never local azd state or deployment secrets.
        Invoke-CheckedCommand tar @(
            "-czf", $archive, "--exclude=__pycache__", "--exclude=*.pyc", "--exclude=*.pyo",
            "--exclude=*.sqlite3", "--exclude=*.sqlite3-*", "--exclude=.env", "--exclude=.env.*",
            "--exclude=.git", "--exclude=.venv", "--exclude=.azure", "-C", $root,
            "Dockerfile", "pyproject.toml", "manage.py", "pisign", "signage", "player"
        ) | Out-Null
        $upload = Invoke-Azure @(
            "rest", "--method", "post", "--url", "$endpoint/listBuildSourceUploadUrl?api-version=2019-04-01",
            "--output", "json"
        ) | ConvertFrom-Json
        if ($upload.uploadUrl -notmatch '^https://' -or [string]::IsNullOrWhiteSpace($upload.relativePath)) {
            throw "Azure did not return a valid build-source upload location."
        }
        try {
            Invoke-WebRequest -Uri $upload.uploadUrl -Method Put -InFile $archive `
                -Headers @{ "x-ms-blob-type" = "BlockBlob" } -ContentType "application/octet-stream" `
                -TimeoutSec 180 | Out-Null
        } catch [Microsoft.PowerShell.Commands.HttpResponseException], [System.Net.Http.HttpRequestException], [System.Net.WebException] {
            throw "ACR build-source upload failed. Check network access to the registry's storage; deployment stopped."
        }
        $task = [IO.File]::ReadAllText((Join-Path $PSScriptRoot "acr-task.yaml"))
        $request = @{
            type = "EncodedTaskRunRequest"
            encodedTaskContent = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($task))
            sourceLocation = $upload.relativePath
            platform = @{ os = "Linux"; architecture = "amd64" }
            timeout = 1800
            credentials = @{ sourceRegistry = @{ loginMode = "Default" } }
            values = @(
                @{ name = "image"; value = $Image; isSecret = $false }
                @{ name = "pip_config"; value = [IO.File]::ReadAllText($pipConfig); isSecret = $true }
            )
        }
        $request | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $requestFile -Encoding utf8
        # A file-backed REST body keeps credentials out of process arguments.
        $run = Invoke-Azure @(
            "rest", "--method", "post", "--url", "$endpoint/scheduleRun?api-version=2019-04-01",
            "--body", "@$requestFile", "--output", "json"
        ) | ConvertFrom-Json
        $runId = $run.properties.runId
        if ($runId -notmatch '^[a-zA-Z0-9-]+$') {
            throw "Azure did not return a valid remote build run ID. No rollout was started."
        }
        $deadline = [DateTime]::UtcNow.AddMinutes(40)
        do {
            $status = (Invoke-Azure @(
                "acr", "task", "show-run", "--registry", $acr, "--run-id", $runId,
                "--query", "status", "--output", "tsv"
            ) | Out-String).Trim()
            Write-Host "Remote build $runId`: $status"
            if ($status -eq "Succeeded") { break }
            if ($status -notin @("Queued", "Started", "Running")) {
                throw "Remote build $runId returned '$status'. Inspect ACR task logs; no migration or web rollout was started."
            }
            if ([DateTime]::UtcNow -ge $deadline) {
                throw "Timed out waiting for remote build $runId. Inspect its status before retrying; no rollout was started."
            }
            Start-Sleep -Seconds 10
        } while ($true)
        Write-Host "Remote build succeeded. View its logs with: az acr task logs --registry $acr --run-id $runId"
    } finally {
        foreach ($file in @($requestFile, $archive)) {
            if (Test-Path -LiteralPath $file) { Remove-Item -LiteralPath $file }
        }
        Remove-Item -LiteralPath $temporary
    }
}

function Assert-SQLDatabaseTarget {
    if ($sqlName -notmatch '^[a-zA-Z][a-zA-Z0-9_-]{0,127}$' -or $sqlName -in @("master", "tempdb", "model", "msdb")) {
        throw "PISIGN_SQL_NAME must be an existing, non-system database name containing only letters, digits, underscores, and hyphens."
    }
    if ($sqlServer -notmatch '^[a-z0-9][a-z0-9-]{0,62}$') {
        throw "PISIGN_SQL_SERVER_NAME must be the logical server name, not a hostname or connection string."
    }
    $server = Invoke-Azure @(
        "sql", "server", "show", "--name", $sqlServer,
        "--resource-group", $sqlResourceGroup, "--output", "json"
    ) | ConvertFrom-Json
    if ($Phase -eq "deploy" -and $server.fullyQualifiedDomainName -ne $sqlHost) {
        throw "PISIGN_SQL_HOST does not match the selected existing logical server."
    }
    $databases = @(Invoke-Azure @(
        "sql", "db", "list", "--server", $sqlServer,
        "--resource-group", $sqlResourceGroup, "--query", "[?name=='$sqlName']", "--output", "json"
    ) | ConvertFrom-Json)
    if ($databases.Count -eq 0) {
        throw "The selected Azure SQL database does not exist. The shared-database deployment never creates or adopts a different database."
    }
    $administrators = @(Invoke-Azure @(
        "sql", "server", "ad-admin", "list", "--server", $sqlServer,
        "--resource-group", $sqlResourceGroup, "--output", "json"
    ) | ConvertFrom-Json)
    if ($administrators.Count -eq 0) {
        throw "The existing SQL server needs an Entra administrator before managed-identity authentication can work. Configure it explicitly; deployment never changes shared server authentication."
    }
}

function Assert-RegistryTarget {
    param([string] $Name, [string] $Group)
    $registryInfo = Invoke-Azure @("acr", "show", "--name", $Name, "--output", "json") | ConvertFrom-Json
    if ($registryInfo.resourceGroup -ne $Group) {
        throw "The selected registry is not in PISIGN_EXISTING_REGISTRY_RESOURCE_GROUP."
    }
    $mode = $registryInfo.PSObject.Properties["roleAssignmentMode"]
    if (-not $mode -or $mode.Value -ne "LegacyRegistryPermissions") {
        throw "The registry must use classic RBAC permissions for AcrPull. ABAC registries are not supported by this deployment; do not change a shared registry's permissions mode. Use a current Azure CLI to verify the mode."
    }
    return $registryInfo
}

function Assert-PiSignOwnership {
    param([object] $Resource)
    $tags = $Resource.PSObject.Properties["tags"]
    $marker = if ($tags -and $tags.Value) { $tags.Value.PSObject.Properties["pisign-managed"] }
    $expectedId = "/subscriptions/$subscription/resourceGroups/$resourceGroup/providers/Microsoft.App/"
    if (-not $marker -or $marker.Value -ne "true" -or -not $Resource.id.StartsWith($expectedId, [StringComparison]::OrdinalIgnoreCase)) {
        throw "A PiSign runtime name is already occupied by an unmanaged resource. Deployment stopped to protect the existing application."
    }
}

function Assert-SharedEnvironment {
    $shared = Invoke-Azure @(
        "containerapp", "env", "show", "--name", $existingEnvironment,
        "--resource-group", $existingEnvironmentGroup, "--output", "json"
    ) | ConvertFrom-Json
    if ($shared.properties.provisioningState -ne "Succeeded") {
        throw "The existing Container Apps environment is not ready."
    }
    if (($shared.location -replace '\s', '') -ne ($location -replace '\s', '')) {
        throw "AZURE_LOCATION must match the existing Container Apps environment's location: $($shared.location)."
    }
    $profilesProperty = $shared.properties.PSObject.Properties["workloadProfiles"]
    $profiles = @(if ($profilesProperty -and $null -ne $profilesProperty.Value) { $profilesProperty.Value })
    $profileName = ""
    if ($profiles.Count -gt 0) {
        if (-not ($profiles | Where-Object { $_.name -eq "Consumption" -and $_.workloadProfileType -eq "Consumption" })) {
            throw "The shared environment must have a Consumption workload profile. Deployment will not modify its existing profiles."
        }
        $profileName = "Consumption"
    }
    $apps = @(Invoke-Azure @(
        "containerapp", "list", "--query",
        "[?(name=='$prefix' || name=='$prefix-documents') && properties.managedEnvironmentId=='$($shared.id)']", "--output", "json"
    ) | ConvertFrom-Json)
    foreach ($app in $apps) { Assert-PiSignOwnership $app }
    return @{ Id = $shared.id; WorkloadProfile = $profileName }
}

$imageSource = [Environment]::GetEnvironmentVariable("PISIGN_IMAGE_SOURCE")
if ([string]::IsNullOrWhiteSpace($imageSource)) { $imageSource = "local" }
if ($imageSource -notin @("local", "remote", "pipeline")) {
    throw "PISIGN_IMAGE_SOURCE must be local, remote, or pipeline."
}
$localBuild = $Phase -eq "deploy" -and $imageSource -eq "local"
$remoteBuild = $Phase -eq "deploy" -and $imageSource -eq "remote"
$tools = @("az")
if ($localBuild) { $tools += "docker" }
if ($remoteBuild) {
    $tools += "tar"
    if (-not $IsWindows) { $tools += "chmod" }
}
foreach ($tool in $tools) {
    if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) {
        throw "$tool is required. Install the prerequisites in deploy\README.md."
    }
}

$subscription = Get-RequiredEnvironmentValue "AZURE_SUBSCRIPTION_ID"
$tenantId = Get-RequiredEnvironmentValue "PISIGN_ENTRA_TENANT_ID"
$subdomain = Get-RequiredEnvironmentValue "PISIGN_ENTRA_SUBDOMAIN"
$clientId = Get-RequiredEnvironmentValue "PISIGN_ENTRA_CLIENT_ID"
$location = Get-RequiredEnvironmentValue "AZURE_LOCATION"
$prefix = if ($Phase -eq "deploy") { Get-RequiredEnvironmentValue "PISIGN_PREFIX" } else { "pisign" }
$databaseEngine = [Environment]::GetEnvironmentVariable("PISIGN_DATABASE_ENGINE")
if ([string]::IsNullOrWhiteSpace($databaseEngine)) { $databaseEngine = "postgresql" }
if ($databaseEngine -notin @("postgresql", "mssql")) {
    throw "Azure deployments support PISIGN_DATABASE_ENGINE=postgresql or mssql. SQLite is only supported locally."
}
$resourceGroup = [Environment]::GetEnvironmentVariable("AZURE_RESOURCE_GROUP")
if ([string]::IsNullOrWhiteSpace($resourceGroup)) {
    $resourceGroup = "rg-$(Get-RequiredEnvironmentValue 'AZURE_ENV_NAME')"
}
if ($databaseEngine -eq "mssql") {
    $sqlServer = Get-RequiredEnvironmentValue "PISIGN_SQL_SERVER_NAME"
    $sqlResourceGroup = Get-RequiredEnvironmentValue "PISIGN_SQL_SERVER_RESOURCE_GROUP"
    $sqlName = Get-RequiredEnvironmentValue "PISIGN_SQL_NAME"
    $sqlHost = if ($Phase -eq "deploy") { Get-RequiredEnvironmentValue "PISIGN_SQL_HOST" } else { "" }
}
$existingEnvironment = [Environment]::GetEnvironmentVariable("PISIGN_EXISTING_ENVIRONMENT_NAME")
$existingRegistry = [Environment]::GetEnvironmentVariable("PISIGN_EXISTING_REGISTRY_NAME")
$reuseEnvironment = -not [string]::IsNullOrWhiteSpace($existingEnvironment)
$reuseRegistry = -not [string]::IsNullOrWhiteSpace($existingRegistry)
if ($reuseEnvironment) {
    if ($databaseEngine -ne "mssql") {
        throw "Reusing a Container Apps environment currently requires mssql and existing SQL connectivity. The PostgreSQL deployment requires its own VNet-integrated environment."
    }
    $existingEnvironmentGroup = Get-RequiredEnvironmentValue "PISIGN_EXISTING_ENVIRONMENT_RESOURCE_GROUP"
    if ($resourceGroup -in @($existingEnvironmentGroup, $sqlResourceGroup)) {
        throw "Use a separate PiSign AZURE_RESOURCE_GROUP, not a shared environment or SQL resource group."
    }
} elseif (-not [string]::IsNullOrWhiteSpace([Environment]::GetEnvironmentVariable("PISIGN_EXISTING_ENVIRONMENT_RESOURCE_GROUP"))) {
    throw "Set PISIGN_EXISTING_ENVIRONMENT_NAME or unset PISIGN_EXISTING_ENVIRONMENT_RESOURCE_GROUP."
}
if ($reuseRegistry) {
    $existingRegistryGroup = Get-RequiredEnvironmentValue "PISIGN_EXISTING_REGISTRY_RESOURCE_GROUP"
    if ($resourceGroup -eq $existingRegistryGroup) {
        throw "Use a separate PiSign AZURE_RESOURCE_GROUP, not the shared registry resource group."
    }
} elseif (-not [string]::IsNullOrWhiteSpace([Environment]::GetEnvironmentVariable("PISIGN_EXISTING_REGISTRY_RESOURCE_GROUP"))) {
    throw "Set PISIGN_EXISTING_REGISTRY_NAME or unset PISIGN_EXISTING_REGISTRY_RESOURCE_GROUP."
}
if ($localBuild -or $remoteBuild) {
    $pipConfig = Get-RequiredEnvironmentValue "PISIGN_PIP_CONFIG"
    if (-not (Test-Path -LiteralPath $pipConfig -PathType Leaf)) {
        throw "PISIGN_PIP_CONFIG must point to an existing approved pip configuration file."
    }
    $pipConfig = (Resolve-Path -LiteralPath $pipConfig).Path
    if ($remoteBuild -and $pipConfig.StartsWith(
        (Split-Path -Parent $PSScriptRoot) + [IO.Path]::DirectorySeparatorChar,
        [StringComparison]::OrdinalIgnoreCase
    )) {
        throw "For remote builds, keep PISIGN_PIP_CONFIG outside the repository so it cannot enter the source upload."
    }
}

Invoke-Azure @("account", "show", "--output", "none") | Out-Null
if ($databaseEngine -eq "mssql") {
    Assert-SQLDatabaseTarget
}
if ($reuseEnvironment) { $sharedEnvironment = Assert-SharedEnvironment }
if ($reuseRegistry) { $sharedRegistry = Assert-RegistryTarget $existingRegistry $existingRegistryGroup }
if ($localBuild) {
    $dockerOs = (Invoke-CheckedCommand docker @("info", "--format", "{{.OSType}}") | Out-String).Trim()
    if ($dockerOs -ne "linux") {
        throw "Docker must be running Linux containers."
    }
    Invoke-CheckedCommand docker @("buildx", "version") | Out-Null
}

if ($Phase -eq "provision") {
    if ($databaseEngine -eq "postgresql") {
        $password = Get-RequiredEnvironmentValue "PISIGN_POSTGRES_PASSWORD"
        if ($password.Length -lt 16) {
            throw "PISIGN_POSTGRES_PASSWORD needs 16+ characters."
        }
    }
    $secret = Get-RequiredEnvironmentValue "PISIGN_SECRET_KEY"
    Get-RequiredEnvironmentValue "PISIGN_ENTRA_CLIENT_SECRET" | Out-Null
    if ($secret.Length -lt 50) {
        throw "PISIGN_SECRET_KEY needs 50+ characters."
    }
    Write-Host "Prerequisites checked. Provisioning infrastructure without changing runtime revisions."
    return
}

$resourceGroup = Get-RequiredEnvironmentValue "AZURE_RESOURCE_GROUP"
$acr = Get-RequiredEnvironmentValue "PISIGN_REGISTRY_NAME"
$registry = Get-RequiredEnvironmentValue "PISIGN_REGISTRY_LOGIN_SERVER"
if ($reuseRegistry -and ($acr -ne $sharedRegistry.name -or $registry -ne $sharedRegistry.loginServer)) {
    throw "Provisioned registry outputs do not match the selected existing registry. Reprovision before deployment."
}
$job = "$prefix-migrate"
$runtime = Join-Path $PSScriptRoot "runtime.bicep"
$root = Split-Path -Parent $PSScriptRoot

# Refuse to change the job's image or start another execution during a migration.
$existingJobs = Invoke-Azure @(
    "containerapp", "job", "list", "--resource-group", $resourceGroup,
    "--query", "[?name=='$job' || name=='$prefix-cleanup']", "--output", "json"
) | ConvertFrom-Json
if ($reuseEnvironment) {
    foreach ($existing in $existingJobs) {
        Assert-PiSignOwnership $existing
        if ($existing.properties.environmentId -ne $sharedEnvironment.Id) {
            throw "An existing PiSign job belongs to a different Container Apps environment."
        }
    }
}
if (@($existingJobs | Where-Object { $_.name -eq $job }).Count -gt 0) {
    $executions = Invoke-Azure @(
        "containerapp", "job", "execution", "list",
        "--name", $job, "--resource-group", $resourceGroup, "--output", "json"
    ) | ConvertFrom-Json
    if (@($executions | Where-Object { $_.properties.status -notin @("Succeeded", "Failed", "Stopped") }).Count -gt 0) {
        throw "A migration execution is already active or has an unknown status. Inspect it before deploying."
    }
}

if ($localBuild -or $remoteBuild) {
    $tag = "{0}-{1}" -f (Get-Date -AsUTC -Format "yyyyMMddHHmmss"), ([guid]::NewGuid().ToString("N"))
    $imageTag = "pisign:$tag"
    if ($remoteBuild) {
        Write-Host "Building and pushing $registry/$imageTag in Azure Container Registry."
        Invoke-RemoteBuild $imageTag
    } else {
        Write-Host "Building and pushing $registry/$imageTag"
        Invoke-Azure @("acr", "login", "--name", $acr) | Out-Host
        Invoke-CheckedCommand docker @(
            "buildx", "build", "--platform", "linux/amd64",
            "--secret", "id=pip_config,src=$pipConfig",
            "--tag", "$registry/$imageTag", "--push", $root
        ) | Out-Host
    }
} else {
    $imageTag = Get-RequiredEnvironmentValue "PISIGN_IMAGE_TAG"
    if ($imageTag -notmatch '^[a-z0-9][a-z0-9._/-]*(?::[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}|@sha256:[0-9a-f]{64})$') {
        throw "PISIGN_IMAGE_TAG must be repository:tag or repository@sha256:digest, without a registry hostname."
    }
}
if (-not $localBuild) {
    $digest = (Invoke-Azure @(
        "acr", "repository", "show", "--name", $acr, "--image", $imageTag,
        "--query", "digest", "--output", "tsv"
    ) | Out-String).Trim()
    if ($digest -notmatch '^sha256:[0-9a-f]{64}$') {
        throw "Azure did not return a valid image digest. No migration or web rollout was started."
    }
    $separator = if ($imageTag.Contains("@")) { "@" } else { ":" }
    $repository = $imageTag.Substring(0, $imageTag.LastIndexOf($separator))
    if ($separator -eq "@" -and $imageTag -ne "$repository@$digest") {
        throw "The registry digest does not match the requested immutable image."
    }
    $imageTag = "$repository@$digest"
    Write-Host "Deploying immutable image $registry/$imageTag"
}

$parameters = @(
    "prefix=$prefix"
    "location=$location"
    "imageTag=$imageTag"
    "entraTenantId=$tenantId"
    "entraSubdomain=$subdomain"
    "entraClientId=$clientId"
    "databaseEngine=$databaseEngine"
    "registryName=$acr"
)
if ($reuseEnvironment) {
    $parameters += @(
        "containerEnvironmentName=$existingEnvironment",
        "containerEnvironmentResourceGroup=$existingEnvironmentGroup",
        "workloadProfileName=$($sharedEnvironment.WorkloadProfile)"
    )
}
if ($reuseRegistry) { $parameters += "registryResourceGroup=$existingRegistryGroup" }
if ($databaseEngine -eq "mssql") {
    $parameters += @("sqlHost=$sqlHost", "sqlDatabaseName=$sqlName")
}
$deployment = @(
    "deployment", "group", "create",
    "--resource-group", $resourceGroup,
    "--template-file", $runtime,
    "--parameters"
) + $parameters

Write-Host "Deploying the migration job. The current web revision remains untouched."
Invoke-Azure ($deployment + @("deployWeb=false", "--name", "pisign-jobs", "--output", "none")) | Out-Null
$execution = (Invoke-Azure @(
    "containerapp", "job", "start", "--name", $job,
    "--resource-group", $resourceGroup, "--query", "name", "--output", "tsv"
) | Out-String).Trim()
if ([string]::IsNullOrWhiteSpace($execution)) {
    throw "Azure did not return a migration execution name. Deployment stopped."
}

$deadline = [DateTime]::UtcNow.AddMinutes(15)
do {
    $status = (Invoke-Azure @(
        "containerapp", "job", "execution", "show", "--name", $job,
        "--resource-group", $resourceGroup, "--job-execution-name", $execution,
        "--query", "properties.status", "--output", "tsv"
    ) | Out-String).Trim()
    Write-Host "Migration $execution`: $status"
    if ($status -eq "Succeeded") {
        break
    }
    if ($status -notin @("Running", "Processing", "Pending")) {
        throw "Migration $execution returned '$status'. Inspect its execution logs; the web rollout was not started."
    }
    if ([DateTime]::UtcNow -ge $deadline) {
        throw "Timed out waiting for migration $execution. It may still be running; inspect it before retrying."
    }
    Start-Sleep -Seconds 10
} while ($true)

Write-Host "Migrations succeeded. Deploying the web app and scheduled cleanup."
Invoke-Azure ($deployment + @("deployWeb=true", "--name", "pisign-runtime", "--output", "none")) | Out-Null
$outputs = Invoke-Azure @(
    "deployment", "group", "show", "--name", "pisign-runtime",
    "--resource-group", $resourceGroup, "--query", "properties.outputs", "--output", "json"
) | ConvertFrom-Json
$appUrl = $outputs.appUrl.value
$redirectUri = $outputs.redirectUri.value
if ([string]::IsNullOrWhiteSpace($appUrl) -or [string]::IsNullOrWhiteSpace($redirectUri)) {
    throw "The runtime deployment did not return the app URL and redirect URI."
}

foreach ($path in @("/health/live/", "/health/ready/")) {
    $health = Invoke-RestMethod "$appUrl$path" -TimeoutSec 15 -MaximumRetryCount 12 -RetryIntervalSec 5
    if ($health.status -ne "ok") {
        throw "Health check $path did not report ok."
    }
}
Write-Host "Deployment healthy: $appUrl"
Write-Host "Register this Web redirect URI in the Entra external-tenant application: $redirectUri"
Write-Host "Then verify sign-in, pairing, playlist changes, and player polling."
