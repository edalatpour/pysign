Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$hookFile = Join-Path (Split-Path -Parent $PSScriptRoot) "azd.ps1"
$hook = {
    param([string] $Phase)
    . $hookFile -Phase $Phase
}

function Assert-True {
    param([bool] $Condition, [string] $Message)
    if (-not $Condition) {
        throw $Message
    }
}

function Get-Command {
    [CmdletBinding()]
    param([string] $Name)
    if ($Name -eq "docker" -and $script:hideDocker) { return }
    Microsoft.PowerShell.Core\Get-Command @PSBoundParameters
}

function az {
    $arguments = @($args)
    $script:calls.Add("az " + ($arguments -join " "))
    if ($arguments[0] -eq "cloud") {
        Assert-True (-not ($arguments -contains "--subscription")) "Cloud metadata does not accept a subscription argument."
        $global:LASTEXITCODE = 0
        return "https://management.azure.com/"
    }
    Assert-True ($arguments[-2] -eq "--subscription" -and $arguments[-1] -eq "test-subscription") "Subscription was not explicit."
    $global:LASTEXITCODE = 0
    $command = $arguments[0..([Math]::Min(3, $arguments.Count - 1))] -join " "
    switch -Wildcard ($command) {
        "account show *" { return }
        "acr login *" { return }
        "acr show *" {
            if ($arguments -contains "id") {
                if ($script:invalidRegistryId) { return "other-registry" }
                return "/subscriptions/test-subscription/resourceGroups/rg-shared/providers/Microsoft.ContainerRegistry/registries/sharedregistry"
            }
            return @{
                name = "sharedregistry"
                resourceGroup = $script:registryGroup
                loginServer = "sharedregistry.azurecr.io"
                roleAssignmentMode = $script:registryMode
            } | ConvertTo-Json
        }
        "acr repository show *" { return $script:digest }
        "rest --method post *" {
            $url = $arguments[[Array]::IndexOf($arguments, "--url") + 1]
            if ($url -like "*/listBuildSourceUploadUrl?api-version=2019-04-01") {
                return '{"uploadUrl":"https://uploads.example.invalid/source.tar.gz","relativePath":"source/test.tar.gz"}'
            }
            Assert-True ($url -like "*/scheduleRun?api-version=2019-04-01") "Unexpected REST request."
            $body = $arguments[[Array]::IndexOf($arguments, "--body") + 1]
            Assert-True ($body.StartsWith("@")) "Secret request was passed on the command line."
            $script:requestPath = $body.Substring(1)
            $script:request = Get-Content -LiteralPath $script:requestPath -Raw | ConvertFrom-Json
            Assert-True ($script:request.type -eq "EncodedTaskRunRequest") "Remote build used the wrong request type."
            Assert-True ($script:request.sourceLocation -eq "source/test.tar.gz") "Build used a different source upload."
            Assert-True ($script:request.platform.os -eq "Linux" -and $script:request.platform.architecture -eq "amd64") "Remote platform was wrong."
            Assert-True ($script:request.credentials.sourceRegistry.loginMode -eq "Default") "Remote registry authentication was missing."
            $secret = @($script:request.values | Where-Object { $_.name -eq "pip_config" })
            Assert-True ($secret.Count -eq 1 -and $secret[0].isSecret -and $secret[0].value -eq [IO.File]::ReadAllText($script:pipConfig)) "Pip configuration was not passed as an ACR secret."
            if ($IsWindows) {
                Assert-True ((Get-Acl -LiteralPath (Split-Path -Parent $script:requestPath)).AreAccessRulesProtected) "Secret directory inherited access rules."
            }
            $task = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($script:request.encodedTaskContent))
            Assert-True ($task.Contains('{{.Values.pip_config | b64enc}}')) "Secret volume encoding was missing."
            Assert-True ($task.Contains("--secret id=pip_config,src=/run/pip-config/pip.conf")) "Required BuildKit secret was missing."
            Assert-True ($task.Contains("DOCKER_BUILDKIT=1") -and $task.Contains("--platform linux/amd64")) "Remote BuildKit/platform configuration was missing."
            Assert-True ($task.Contains('mountPath: /run/pip-config') -and $task.Contains('push:')) "Secret mount or image push was missing."
            if ($script:failSchedule) { $global:LASTEXITCODE = 1 }
            return ('{"properties":{"runId":"' + $script:buildRunId + '"}}')
        }
        "acr task show-run *" { return $script:buildStatuses.Dequeue() }
        "containerapp env show *" {
            return @{
                id = $script:environmentId
                location = $script:environmentLocation
                properties = @{
                    provisioningState = $script:environmentState
                    workloadProfiles = $script:profiles
                }
            } | ConvertTo-Json -Depth 5
        }
        "containerapp list *" {
            return ConvertTo-Json -InputObject @($script:sharedApps) -Depth 6
        }
        "sql server show *" {
            return '{"location":"centralus","fullyQualifiedDomainName":"existing.database.windows.net"}'
        }
        "sql server ad-admin list" {
            if (-not $script:sqlEntraAdmin) { return "[]" }
            return '[{"administratorType":"ActiveDirectory"}]'
        }
        "sql db list *" {
            if (-not $script:sqlDbExists) { return "[]" }
            return @(@{
                name = "pisign"
                collation = "SQL_Latin1_General_CP1_CI_AS"
                useFreeLimit = $false
            }) | ConvertTo-Json -Depth 4 -AsArray
        }
        "containerapp job list *" {
            if ($script:existingJob) {
                return @(@{
                    name = "pisign-migrate"
                    id = "/subscriptions/test-subscription/resourceGroups/rg-test/providers/Microsoft.App/jobs/pisign-migrate"
                    tags = @{ "pisign-managed" = $script:jobOwned }
                    properties = @{ environmentId = $script:jobEnvironment }
                }) | ConvertTo-Json -Depth 5 -AsArray
            }
            return "[]"
        }
        "containerapp job execution list" {
            return ('[{"properties":{"status":"' + $script:existingStatus + '"}}]')
        }
        "containerapp job start *" { return $script:executionName }
        "containerapp job execution show" {
            return $script:statuses.Dequeue()
        }
        "deployment group create *" {
            $script:deployments.Add($arguments)
            $parameterIndex = [Array]::IndexOf($arguments, "--parameters")
            $nameIndex = [Array]::IndexOf($arguments, "--name")
            $webIndex = [Array]::FindIndex($arguments, [Predicate[object]] { param($item) $item -like "deployWeb=*" })
            Assert-True ($parameterIndex -lt $webIndex -and $webIndex -lt $nameIndex) "deployWeb must be inside --parameters."
            Assert-True ($arguments -contains "location=centralus") "Runtime location was not Central US."
            if ($script:failJobs -and $arguments -contains "deployWeb=false") {
                $global:LASTEXITCODE = 1
            }
            return
        }
        "deployment group show *" {
            return '{"appUrl":{"value":"https://test.example"},"redirectUri":{"value":"https://test.example/accounts/callback/"}}'
        }
        default { throw "Unexpected Azure command: $($arguments -join ' ')" }
    }
}

function tar {
    $script:calls.Add("tar " + ($args -join " "))
    $script:archivePath = $args[[Array]::IndexOf($args, "-czf") + 1]
    if ($script:failArchive) { $global:LASTEXITCODE = 1; return }
    $native = Microsoft.PowerShell.Core\Get-Command tar -CommandType Application
    & $native.Source @args
}

function Invoke-WebRequest {
    param([string] $Uri, [string] $Method, [string] $InFile, [hashtable] $Headers, [string] $ContentType, [int] $TimeoutSec)
    $script:calls.Add("upload source")
    Assert-True ($Uri -eq "https://uploads.example.invalid/source.tar.gz" -and $Method -eq "Put") "Incorrect source upload."
    Assert-True ($Headers["x-ms-blob-type"] -eq "BlockBlob") "Upload was not a block blob."
    $native = Microsoft.PowerShell.Core\Get-Command tar -CommandType Application
    $files = @(& $native.Source -tzf $InFile)
    Assert-True ($LASTEXITCODE -eq 0 -and $files -contains "Dockerfile" -and $files -contains "manage.py") "Archive omitted application inputs."
    Assert-True (-not ($files -match '(^|/)(deploy|\.azure|\.git|\.venv|__pycache__|\.env)(/|$)|\.sqlite3|\.py[co]$')) "Archive included deployment state, secrets, databases, or caches."
    Assert-True (-not ($files -match 'request\.json|source\.tar\.gz')) "Secret request was uploaded with build source."
    if ($script:failUpload) {
        throw [System.Net.Http.HttpRequestException]::new("Synthetic upload failure")
    }
}

function docker {
    $script:calls.Add("docker " + ($args -join " "))
    $global:LASTEXITCODE = 0
    switch ($args[0]) {
        "info" { return $script:dockerOs }
        "buildx" {
            if ($args[1] -eq "version") { return "buildx test" }
            Assert-True ($args -contains "linux/amd64") "Image platform was not linux/amd64."
            Assert-True ($args -contains "id=pip_config,src=$script:pipConfig") "BuildKit secret was missing."
            Assert-True ($args -contains "--push") "Image was not pushed."
            if ($script:failBuild) { $global:LASTEXITCODE = 1 }
            return
        }
        default { throw "Unexpected Docker command." }
    }
}

function Start-Sleep {
    param([int] $Seconds)
    $script:calls.Add("sleep $Seconds")
    if ($script:timeoutMigration -or $script:timeoutRemoteBuild) {
        Set-Variable -Name deadline -Value ([DateTime]::MinValue) -Scope 1
    }
}

function Invoke-RestMethod {
    param(
        [string] $Uri,
        [int] $TimeoutSec,
        [int] $MaximumRetryCount,
        [int] $RetryIntervalSec
    )
    $script:calls.Add("health $Uri")
    if ($script:failHealth) { throw "Mock health check failed." }
    return @{ status = "ok" }
}

function Reset-Scenario {
    $script:calls = [Collections.Generic.List[string]]::new()
    $script:deployments = [Collections.Generic.List[object]]::new()
    $script:statuses = [Collections.Generic.Queue[string]]::new()
    $script:statuses.Enqueue("Running")
    $script:statuses.Enqueue("Succeeded")
    $script:existingJob = $false
    $script:existingStatus = "Succeeded"
    $script:executionName = "migration-test"
    $script:dockerOs = "linux"
    $script:failBuild = $false
    $script:failJobs = $false
    $script:failHealth = $false
    $script:timeoutMigration = $false
    $script:sqlDbExists = $true
    $script:sqlEntraAdmin = $true
    $script:hideDocker = $false
    $script:failArchive = $false
    $script:failUpload = $false
    $script:failSchedule = $false
    $script:invalidRegistryId = $false
    $script:timeoutRemoteBuild = $false
    $script:buildRunId = "build-test"
    $script:buildStatuses = [Collections.Generic.Queue[string]]::new()
    foreach ($status in @("Queued", "Started", "Running", "Succeeded")) {
        $script:buildStatuses.Enqueue($status)
    }
    $script:requestPath = ""
    $script:archivePath = ""
    $script:registryGroup = "rg-shared"
    $script:registryMode = "LegacyRegistryPermissions"
    $script:digest = "sha256:" + ("a" * 64)
    $script:environmentId = "/subscriptions/test-subscription/resourceGroups/rg-shared/providers/Microsoft.App/managedEnvironments/shared-env"
    $script:environmentLocation = "centralus"
    $script:environmentState = "Succeeded"
    $script:profiles = @(@{ name = "Consumption"; workloadProfileType = "Consumption" })
    $script:sharedApps = @()
    $script:jobOwned = "true"
    $script:jobEnvironment = $script:environmentId
}

function Assert-Throws {
    param([scriptblock] $Action, [string] $Pattern)
    $message = ""
    try { & $Action | Out-Null } catch { $message = $_.Exception.Message }
    Assert-True ($message -like $Pattern) "Expected '$Pattern'; got '$message'."
}

$names = @(
    "AZURE_SUBSCRIPTION_ID", "AZURE_RESOURCE_GROUP", "AZURE_LOCATION",
    "PISIGN_PREFIX", "PISIGN_REGISTRY_NAME", "PISIGN_REGISTRY_LOGIN_SERVER",
    "PISIGN_ENTRA_TENANT_ID", "PISIGN_ENTRA_SUBDOMAIN", "PISIGN_ENTRA_CLIENT_ID",
    "PISIGN_PIP_CONFIG", "PISIGN_POSTGRES_PASSWORD", "PISIGN_SECRET_KEY",
    "PISIGN_ENTRA_CLIENT_SECRET", "PISIGN_DATABASE_ENGINE", "PISIGN_SQL_SERVER_NAME",
    "PISIGN_SQL_SERVER_RESOURCE_GROUP", "PISIGN_SQL_NAME", "PISIGN_SQL_HOST",
    "PISIGN_IMAGE_SOURCE", "PISIGN_IMAGE_TAG", "PISIGN_EXISTING_ENVIRONMENT_NAME",
    "PISIGN_EXISTING_ENVIRONMENT_RESOURCE_GROUP", "PISIGN_EXISTING_REGISTRY_NAME",
    "PISIGN_EXISTING_REGISTRY_RESOURCE_GROUP"
)
$saved = @{}
foreach ($name in $names) { $saved[$name] = [Environment]::GetEnvironmentVariable($name) }
$pipConfig = [IO.Path]::GetTempFileName()

try {
    [IO.File]::WriteAllText($pipConfig, "[global]`nindex-url = https://synthetic-user:synthetic-test-only@example.invalid/simple`n")
    foreach ($name in $names | Where-Object { $_ -like "PISIGN_EXISTING_*" -or $_ -like "PISIGN_IMAGE_*" }) {
        [Environment]::SetEnvironmentVariable($name, "")
    }
    $env:AZURE_SUBSCRIPTION_ID = "test-subscription"
    $env:AZURE_RESOURCE_GROUP = "rg-test"
    $env:AZURE_LOCATION = "centralus"
    $env:PISIGN_PREFIX = "pisign"
    $env:PISIGN_REGISTRY_NAME = "testregistry"
    $env:PISIGN_REGISTRY_LOGIN_SERVER = "testregistry.azurecr.io"
    $env:PISIGN_ENTRA_TENANT_ID = "test-tenant"
    $env:PISIGN_ENTRA_SUBDOMAIN = "test-subdomain"
    $env:PISIGN_ENTRA_CLIENT_ID = "test-client"
    $env:PISIGN_PIP_CONFIG = $pipConfig
    $env:PISIGN_POSTGRES_PASSWORD = "test-password-123456"
    $env:PISIGN_SECRET_KEY = "s" * 64
    $env:PISIGN_ENTRA_CLIENT_SECRET = "test-client-secret"
    $env:PISIGN_DATABASE_ENGINE = "postgresql"
    $env:PISIGN_SQL_SERVER_NAME = "existing"
    $env:PISIGN_SQL_SERVER_RESOURCE_GROUP = "existing-rg"
    $env:PISIGN_SQL_NAME = "pisign"
    $env:PISIGN_SQL_HOST = "existing.database.windows.net"

    Reset-Scenario
    $hideDocker = $true
    $env:PISIGN_PIP_CONFIG = ""
    & $hook -Phase provision
    Assert-True ($deployments.Count -eq 0) "Preprovision changed runtime resources."
    Assert-True (-not ($calls -match "^docker ")) "Provisioning required Docker."
    $env:PISIGN_PIP_CONFIG = $pipConfig

    Reset-Scenario
    & $hook -Phase deploy
    Assert-True ($deployments.Count -eq 2) "First deployment did not deploy jobs and runtime."
    Assert-True (-not ($calls -match "execution list")) "First deployment queried executions of a nonexistent job."
    Assert-True (($calls -match "^health ").Count -eq 2) "Both health checks must run."
    $migrationIndex = $calls.FindIndex([Predicate[string]] { param($call) $call -like "*execution show*" })
    $runtimeIndex = $calls.FindIndex([Predicate[string]] { param($call) $call -like "*deployWeb=true*" })
    Assert-True ($runtimeIndex -gt $migrationIndex) "Runtime deployed before migration polling."
    $jobsImage = $deployments[0] | Where-Object { $_ -like "imageTag=*" }
    $runtimeImage = $deployments[1] | Where-Object { $_ -like "imageTag=*" }
    Assert-True ($jobsImage -eq $runtimeImage) "Jobs and runtime used different images."

    Reset-Scenario
    $existingJob = $true
    & $hook -Phase deploy
    Assert-True ($deployments.Count -eq 2) "Redeployment did not complete."
    $nextImage = $deployments[0] | Where-Object { $_ -like "imageTag=*" }
    Assert-True ($nextImage -ne $jobsImage) "Image tags were reused."

    Reset-Scenario
    $existingJob = $true
    $existingStatus = "Running"
    Assert-Throws { & $hook -Phase deploy } "*migration execution is already active*"
    Assert-True ($deployments.Count -eq 0 -and -not ($calls -match "buildx build")) "An active migration was modified."

    Reset-Scenario
    $failBuild = $true
    Assert-Throws { & $hook -Phase deploy } "*docker failed with exit code 1*"
    Assert-True ($deployments.Count -eq 0) "Build failure did not stop deployment."

    Reset-Scenario
    $failJobs = $true
    Assert-Throws { & $hook -Phase deploy } "*az failed with exit code 1*"
    Assert-True (-not ($calls -match "job start")) "Migration started after deployment failure."

    foreach ($status in @("Failed", "Stopped", "")) {
        Reset-Scenario
        $statuses.Clear()
        $statuses.Enqueue($status)
        Assert-Throws { & $hook -Phase deploy } "*web rollout was not started*"
        Assert-True ($deployments.Count -eq 1) "Unsuccessful migration allowed web rollout."
    }

    Reset-Scenario
    $timeoutMigration = $true
    $statuses.Clear()
    $statuses.Enqueue("Running")
    $statuses.Enqueue("Running")
    Assert-Throws { & $hook -Phase deploy } "*Timed out waiting for migration*"
    Assert-True ($deployments.Count -eq 1) "Migration timeout allowed web rollout."

    Reset-Scenario
    $executionName = ""
    Assert-Throws { & $hook -Phase deploy } "*did not return a migration execution name*"
    Assert-True ($deployments.Count -eq 1) "Empty execution name allowed web rollout."

    Reset-Scenario
    $failHealth = $true
    Assert-Throws { & $hook -Phase deploy } "*Mock health check failed*"

    Reset-Scenario
    $dockerOs = "windows"
    Assert-Throws { & $hook -Phase deploy } "*Linux containers*"

    Reset-Scenario
    $env:PISIGN_POSTGRES_PASSWORD = ""
    Assert-Throws { & $hook -Phase provision } "*Set PISIGN_POSTGRES_PASSWORD*"
    & $hook -Phase deploy
    Assert-True ($deployments.Count -eq 2) "Deployment required local provisioning secrets."

    $env:PISIGN_DATABASE_ENGINE = "mssql"
    Reset-Scenario
    $sqlDbExists = $false
    Assert-Throws { & $hook -Phase provision } "*selected Azure SQL database does not exist*"
    Assert-Throws { & $hook -Phase deploy } "*selected Azure SQL database does not exist*"
    Assert-True ($deployments.Count -eq 0) "Missing shared database allowed runtime changes."

    Reset-Scenario
    $sqlEntraAdmin = $false
    Assert-Throws { & $hook -Phase provision } "*SQL server needs an Entra administrator*"
    Assert-Throws { & $hook -Phase deploy } "*SQL server needs an Entra administrator*"
    Assert-True ($deployments.Count -eq 0 -and -not ($calls -match "^docker |^tar ")) "Missing SQL Entra configuration allowed a build or rollout."

    Reset-Scenario
    & $hook -Phase provision
    & $hook -Phase deploy
    Assert-True ($deployments.Count -eq 2) "SQL deployment did not complete."
    foreach ($deployment in $deployments) {
        Assert-True ($deployment -contains "databaseEngine=mssql") "SQL backend was not selected."
        Assert-True ($deployment -contains "sqlHost=existing.database.windows.net") "SQL server host was not passed."
        Assert-True ($deployment -contains "sqlDatabaseName=pisign") "Selected shared SQL database was not passed."
    }
    Assert-True (-not ($calls -match "sql (server|db) (create|update|delete)")) "The hook mutated SQL resources directly."

    Reset-Scenario
    $env:PISIGN_SQL_HOST = "other.database.windows.net"
    Assert-Throws { & $hook -Phase deploy } "*does not match the selected existing logical server*"
    $env:PISIGN_SQL_HOST = "existing.database.windows.net"

    foreach ($name in @("master", "other' OR '1'='1", "")) {
        Reset-Scenario
        $env:PISIGN_SQL_NAME = $name
        if ($name -eq "") {
            Assert-Throws { & $hook -Phase provision } "*Set PISIGN_SQL_NAME*"
        } else {
            Assert-Throws { & $hook -Phase provision } "*existing, non-system database name*"
        }
    }

    $env:PISIGN_SQL_NAME = "pisign"
    $env:PISIGN_IMAGE_SOURCE = "pipeline"
    $env:PISIGN_IMAGE_TAG = "pisign:release-123"
    $env:PISIGN_EXISTING_ENVIRONMENT_NAME = "shared-env"
    $env:PISIGN_EXISTING_ENVIRONMENT_RESOURCE_GROUP = "rg-shared"
    $env:PISIGN_EXISTING_REGISTRY_NAME = "sharedregistry"
    $env:PISIGN_EXISTING_REGISTRY_RESOURCE_GROUP = "rg-shared"
    $env:PISIGN_REGISTRY_NAME = "sharedregistry"
    $env:PISIGN_REGISTRY_LOGIN_SERVER = "sharedregistry.azurecr.io"
    $env:PISIGN_PIP_CONFIG = ""

    Reset-Scenario
    $hideDocker = $true
    & $hook -Phase provision
    & $hook -Phase deploy
    Assert-True ($deployments.Count -eq 2) "Pipeline deployment did not complete."
    Assert-True (-not ($calls -match "^docker |acr login")) "Pipeline mode required Docker or registry build login."
    foreach ($deployment in $deployments) {
        Assert-True ($deployment -contains "imageTag=pisign@$digest") "Pipeline image was not pinned to the registry digest."
        Assert-True ($deployment -contains "containerEnvironmentName=shared-env") "Shared environment name was not forwarded."
        Assert-True ($deployment -contains "containerEnvironmentResourceGroup=rg-shared") "Shared environment group was not forwarded."
        Assert-True ($deployment -contains "registryName=sharedregistry") "Shared registry name was not forwarded."
        Assert-True ($deployment -contains "registryResourceGroup=rg-shared") "Shared registry group was not forwarded."
        Assert-True ($deployment -contains "workloadProfileName=Consumption") "Consumption profile was not forwarded."
    }

    Reset-Scenario
    $profiles = $null
    & $hook -Phase deploy
    Assert-True ($deployments[0] -contains "workloadProfileName=") "Legacy consumption-only environment was not supported."

    Reset-Scenario
    $environmentLocation = "Central US"
    & $hook -Phase deploy
    Assert-True ($deployments.Count -eq 2) "Equivalent Azure region display name was rejected."

    Reset-Scenario
    $existingJob = $true
    & $hook -Phase deploy
    Assert-True ($deployments.Count -eq 2) "Owned migration job could not be redeployed."

    Reset-Scenario
    $environmentLocation = "eastus"
    Assert-Throws { & $hook -Phase provision } "*AZURE_LOCATION must match*"
    Reset-Scenario
    $environmentState = "Updating"
    Assert-Throws { & $hook -Phase provision } "*environment is not ready*"
    Reset-Scenario
    $profiles = @(@{ name = "Dedicated"; workloadProfileType = "D4" })
    Assert-Throws { & $hook -Phase provision } "*must have a Consumption workload profile*"
    Reset-Scenario
    $registryGroup = "other-rg"
    Assert-Throws { & $hook -Phase provision } "*not in PISIGN_EXISTING_REGISTRY_RESOURCE_GROUP*"
    Reset-Scenario
    $registryMode = "AbacRepositoryPermissions"
    Assert-Throws { & $hook -Phase provision } "*ABAC registries are not supported*"

    Reset-Scenario
    $sharedApps = @(@{
        id = "/subscriptions/test-subscription/resourceGroups/rg-shared/providers/Microsoft.App/containerApps/pisign"
        name = "pisign"
        tags = @{ "pisign-managed" = "true" }
    })
    Assert-Throws { & $hook -Phase provision } "*occupied by an unmanaged resource*"
    Reset-Scenario
    $existingJob = $true
    $jobOwned = "false"
    Assert-Throws { & $hook -Phase deploy } "*occupied by an unmanaged resource*"
    Assert-True ($deployments.Count -eq 0) "An unowned job was overwritten."
    Reset-Scenario
    $existingJob = $true
    $jobEnvironment = "other-environment"
    Assert-Throws { & $hook -Phase deploy } "*belongs to a different Container Apps environment*"

    Reset-Scenario
    $env:AZURE_RESOURCE_GROUP = "rg-shared"
    Assert-Throws { & $hook -Phase provision } "*Use a separate PiSign AZURE_RESOURCE_GROUP*"
    $env:AZURE_RESOURCE_GROUP = "rg-test"
    $env:PISIGN_DATABASE_ENGINE = "postgresql"
    Assert-Throws { & $hook -Phase provision } "*currently requires mssql*"
    $env:PISIGN_DATABASE_ENGINE = "mssql"

    Reset-Scenario
    $env:PISIGN_IMAGE_TAG = ""
    Assert-Throws { & $hook -Phase deploy } "*Set PISIGN_IMAGE_TAG*"
    Assert-True ($deployments.Count -eq 0) "A missing pipeline image started rollout."
    $env:PISIGN_IMAGE_TAG = "pisign:release-123"
    Reset-Scenario
    $digest = "not-a-digest"
    Assert-Throws { & $hook -Phase deploy } "*did not return a valid image digest*"
    Assert-True ($deployments.Count -eq 0) "An invalid digest started rollout."
    Reset-Scenario
    $env:PISIGN_IMAGE_TAG = "pisign@sha256:" + ("b" * 64)
    Assert-Throws { & $hook -Phase deploy } "*does not match the requested immutable image*"
    $env:PISIGN_IMAGE_TAG = "pisign:release-123"
    Reset-Scenario
    $statuses.Clear()
    $statuses.Enqueue("Failed")
    Assert-Throws { & $hook -Phase deploy } "*web rollout was not started*"
    Assert-True ($deployments.Count -eq 1) "Pipeline migration failure allowed web rollout."

    $env:PISIGN_IMAGE_SOURCE = "remote"
    $env:PISIGN_PIP_CONFIG = $pipConfig
    Reset-Scenario
    $hideDocker = $true
    & $hook -Phase provision
    & $hook -Phase deploy
    Assert-True ($deployments.Count -eq 2) "Remote build did not complete deployment."
    Assert-True (-not ($calls -match "^docker |acr login")) "Remote build required local Docker."
    Assert-True ($calls.FindIndex([Predicate[string]] { param($call) $call -like "*acr repository show*" }) -gt $calls.FindIndex([Predicate[string]] { param($call) $call -like "*acr task show-run*" })) "Image was resolved before the build completed."
    foreach ($deployment in $deployments) {
        Assert-True ($deployment -contains "imageTag=pisign@$digest") "Remote image was not digest-pinned."
    }
    Assert-True (-not ($calls -match "synthetic-user|synthetic-test-only")) "Pip credentials appeared in process arguments."
    Assert-True (-not (Test-Path -LiteralPath $requestPath) -and -not (Test-Path -LiteralPath (Split-Path -Parent $requestPath))) "Secret request/directory was not cleaned up."

    foreach ($status in @("Failed", "Canceled", "Error", "Timeout", "", "Unknown")) {
        Reset-Scenario
        $hideDocker = $true
        $buildStatuses.Clear()
        $buildStatuses.Enqueue($status)
        Assert-Throws { & $hook -Phase deploy } "*no migration or web rollout was started*"
        Assert-True ($deployments.Count -eq 0 -and -not ($calls -match "acr repository show")) "Failed remote build allowed rollout."
        Assert-True (-not (Test-Path -LiteralPath $requestPath) -and -not (Test-Path -LiteralPath $archivePath)) "Failed build leaked temporary files."
    }
    foreach ($failure in @("failArchive", "failUpload", "failSchedule")) {
        Reset-Scenario
        Set-Variable -Name $failure -Value $true
        Assert-Throws { & $hook -Phase deploy } "*failed*"
        Assert-True ($deployments.Count -eq 0) "Remote preparation failure allowed rollout."
        Assert-True (-not (Test-Path -LiteralPath (Split-Path -Parent $archivePath))) "Failed remote preparation leaked its temporary directory."
    }
    Reset-Scenario
    $timeoutRemoteBuild = $true
    Assert-Throws { & $hook -Phase deploy } "*Timed out waiting for remote build*"
    Assert-True ($deployments.Count -eq 0 -and -not (Test-Path -LiteralPath $requestPath)) "Timed-out build allowed rollout or leaked its request."
    Reset-Scenario
    $buildRunId = ""
    Assert-Throws { & $hook -Phase deploy } "*valid remote build run ID*"
    Assert-True ($deployments.Count -eq 0 -and -not (Test-Path -LiteralPath $requestPath)) "Missing run ID allowed rollout or leaked its request."
    Reset-Scenario
    $invalidRegistryId = $true
    Assert-Throws { & $hook -Phase deploy } "*selected registry's resource ID*"
    Assert-True ($deployments.Count -eq 0 -and -not ($calls -match "^tar ")) "Invalid registry allowed upload or rollout."
    Reset-Scenario
    $env:PISIGN_PIP_CONFIG = $hookFile
    Assert-Throws { & $hook -Phase deploy } "*outside the repository*"
    Assert-True (-not ($calls -match "^tar ")) "Repository-local pip configuration was uploaded."
    $env:PISIGN_PIP_CONFIG = ""
    Assert-Throws { & $hook -Phase deploy } "*Set PISIGN_PIP_CONFIG*"
    $env:PISIGN_PIP_CONFIG = $pipConfig
    Reset-Scenario
    $digest = "not-a-digest"
    Assert-Throws { & $hook -Phase deploy } "*valid image digest*"
    Assert-True ($deployments.Count -eq 0) "Invalid remote digest allowed rollout."
    Reset-Scenario
    $existingJob = $true
    $existingStatus = "Running"
    Assert-Throws { & $hook -Phase deploy } "*migration execution is already active*"
    Assert-True (-not ($calls -match "^tar |scheduleRun")) "Active migration allowed a remote build."
    Reset-Scenario
    $statuses.Clear()
    $statuses.Enqueue("Failed")
    Assert-Throws { & $hook -Phase deploy } "*web rollout was not started*"
    Assert-True ($deployments.Count -eq 1) "Remote image migration failure allowed web rollout."

    Write-Host "All PostgreSQL, Azure SQL, shared-environment, and local/pipeline/remote deployment hook tests passed."
} finally {
    foreach ($name in $names) { [Environment]::SetEnvironmentVariable($name, $saved[$name]) }
    Remove-Item -LiteralPath $pipConfig
}
