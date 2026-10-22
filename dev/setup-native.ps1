[CmdletBinding()]
param(
    [switch] $Check,
    [switch] $SkipDependencies,
    [switch] $NoStorageDownload,
    [string] $PostgresHost,
    [int] $PostgresPort = 0,
    [string] $RedisHost,
    [int] $RedisPort = 0,
    [string] $RabbitHost,
    [int] $RabbitPort = 0,
    [string] $PostgresAdminUser = 'postgres',
    [AllowEmptyString()]
    [string] $PostgresAdminPassword = '',
    [int] $RabbitManagementPort = 15672,
    [string] $RabbitManagementUser = 'guest',
    [AllowEmptyString()]
    [string] $RabbitManagementPassword = 'guest',
    [string] $StorageExecutable = $env:MINIO_EXE,
    [int] $StartupTimeoutSeconds = 120
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

$moduleRoot = $PSScriptRoot
Import-Module (Join-Path $moduleRoot 'NativeDev.Common.psm1') -Force
Import-Module (Join-Path $moduleRoot 'NativeDev.Environment.psm1') -Force

$context = New-NativeContext -RepoRoot (Get-NativeRepoRoot -StartPath $moduleRoot)
if (-not $Check) {
    Enable-NativeOspServices -Context $context
}
function Find-OspListener {
    param(
        [Parameter(Mandatory = $true)]
        [string] $ProcessPathPattern,
        [Parameter(Mandatory = $true)]
        [int] $Port
    )

    foreach ($listener in @(Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue)) {
        $process = Get-Process -Id $listener.OwningProcess -ErrorAction SilentlyContinue
        $path = $null
        try {
            $path = $process.Path
        } catch {
        }
        if ($path -and $path -match $ProcessPathPattern) {
            return [pscustomobject]@{ HostName = $listener.LocalAddress; Port = $listener.LocalPort }
        }
    }
    return $null
}

$environmentFiles = Get-NativeEnvironmentFiles -Context $context
$missingEnvironments = @($environmentFiles | Where-Object { -not (Test-Path -LiteralPath $_.Path -PathType Leaf) })
if ($missingEnvironments.Count -gt 0) {
    if ($Check) {
        throw 'Environment files are missing; check mode is read-only.'
    }
    if (-not $PostgresHost -or $PostgresPort -le 0) {
        $endpoint = Find-OspListener -ProcessPathPattern '\\PostgreSQL-15\\bin\\postgres\.exe$' -Port 5432
        if (-not $endpoint) {
            throw 'PostgreSQL-15 endpoint was not found; start OSPanel PostgreSQL-15 or pass -PostgresHost/-PostgresPort.'
        }
        if (-not $PostgresHost) { $PostgresHost = $endpoint.HostName }
        if ($PostgresPort -le 0) { $PostgresPort = $endpoint.Port }
    }
    if (-not $RedisHost -or $RedisPort -le 0) {
        $endpoint = Find-OspListener -ProcessPathPattern '\\Redis\\redis-server\.exe$' -Port 6379
        if (-not $endpoint) {
            throw 'Redis endpoint was not found; start OSPanel Redis or pass -RedisHost/-RedisPort.'
        }
        if (-not $RedisHost) { $RedisHost = $endpoint.HostName }
        if ($RedisPort -le 0) { $RedisPort = $endpoint.Port }
    }
    if (-not $RabbitHost -or $RabbitPort -le 0) {
        $rabbitConfig = 'C:\OSPanel\modules\RabbitMQ-3.13\etc\rabbitmq.conf'
        $match = Select-String -LiteralPath $rabbitConfig -Pattern '^\s*listeners\.tcp\.default\s*=\s*([^:]+):(\d+)\s*$' | Select-Object -First 1
        if (-not $match) {
            throw 'RabbitMQ endpoint was not found; start OSPanel RabbitMQ or pass -RabbitHost/-RabbitPort.'
        }
        if (-not $RabbitHost) { $RabbitHost = $match.Matches[0].Groups[1].Value }
        if ($RabbitPort -le 0) { $RabbitPort = [int] $match.Matches[0].Groups[2].Value }
    }

    $generated = New-NativeEnvironmentConfiguration `
        -Context $context `
        -PostgresHost $PostgresHost `
        -PostgresPort $PostgresPort `
        -PostgresUser 'plane_native' `
        -PostgresPassword (New-NativeRandomSecret -ByteCount 36) `
        -PostgresDatabase 'plane' `
        -RedisHost $RedisHost `
        -RedisPort $RedisPort `
        -RabbitHost $RabbitHost `
        -RabbitPort $RabbitPort `
        -RabbitUser 'plane_native' `
        -RabbitPassword (New-NativeRandomSecret -ByteCount 36) `
        -RabbitVHost 'plane-native'
    Write-NativeStatus -Level OK -Message ("Generated {0} missing environment files." -f $generated.Created.Count)
}
$environmentValidation = Test-NativeEnvironmentConfiguration -Context $context
if (-not $environmentValidation.IsValid) {
    $environmentValidation.Failures | ForEach-Object { Write-NativeStatus -Level ERROR -Message $_ }
    throw 'All six environment files must exist and validate before setup or check.'
}
if (-not $Check) {
    $endpointConfiguration = Update-NativeApiEndpointConfiguration -Context $context
    if ($endpointConfiguration.Updated.Count -gt 0) {
        Write-NativeStatus -Level OK -Message ("Updated the native API endpoint in {0} managed environment file(s)." -f $endpointConfiguration.Updated.Count)
    }
}

$apiEnvPath = Join-Path $context.ApiRoot '.env'
$loopbackTargets = Assert-NativeLocalEnvironment -ApiEnvPath $apiEnvPath
if ($Check) {
    $loopbackTargets | Format-Table -AutoSize
    & (Join-Path $moduleRoot 'check-native.ps1') -InfrastructureOnly
    exit $LASTEXITCODE
}
Initialize-NativeContext -Context $context

function Get-RabbitManagementStatus {
    param(
        [Parameter(Mandatory = $true)]
        [string] $Method,
        [Parameter(Mandatory = $true)]
        [string] $Path,
        $Body = $null
    )

    $pair = '{0}:{1}' -f $RabbitManagementUser, $RabbitManagementPassword
    $headers = @{ Authorization = 'Basic ' + [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($pair)) }
    $uri = 'http://{0}:{1}{2}' -f $rabbitManagementHost, $RabbitManagementPort, $Path
    try {
        $parameters = @{
            Uri = $uri
            Method = $Method
            Headers = $headers
            TimeoutSec = 10
            UseBasicParsing = $true
        }
        if ($null -ne $Body) {
            $parameters.ContentType = 'application/json'
            $parameters.Body = ($Body | ConvertTo-Json -Compress)
        }
        $response = Invoke-WebRequest @parameters
        return [int] $response.StatusCode
    } catch {
        $statusCode = $null
        try {
            if ($_.Exception.Response) {
                $statusCode = [int] $_.Exception.Response.StatusCode
            }
        } catch {
        }
        if ($statusCode) {
            return [int] $statusCode
        }
        throw 'RabbitMQ management API is unavailable or rejected guest read access.'
    }
}

function Ensure-RabbitResources {
    param(
        [Parameter(Mandatory = $true)]
        [string] $User,
        [Parameter(Mandatory = $true)]
        [string] $Password,
        [Parameter(Mandatory = $true)]
        [string] $VHost
    )

    if ((Get-RabbitManagementStatus -Method Get -Path '/api/overview') -ne 200) {
        throw 'RabbitMQ management overview did not return HTTP 200.'
    }
    $encodedVHost = [Uri]::EscapeDataString($VHost)
    $encodedUser = [Uri]::EscapeDataString($User)

    $vhostStatus = Get-RabbitManagementStatus -Method Get -Path ('/api/vhosts/{0}' -f $encodedVHost)
    if ($vhostStatus -eq 404) {
        [void] (Get-RabbitManagementStatus -Method Put -Path ('/api/vhosts/{0}' -f $encodedVHost) -Body @{})
    }

    $userStatus = Get-RabbitManagementStatus -Method Get -Path ('/api/users/{0}' -f $encodedUser)
    if ($userStatus -eq 404) {
        [void] (Get-RabbitManagementStatus -Method Put -Path ('/api/users/{0}' -f $encodedUser) -Body @{
            password = $Password
            tags = ''
        })
    }

    [void] (Get-RabbitManagementStatus -Method Get -Path ('/api/permissions/{0}/{1}' -f $encodedVHost, $encodedUser))
    [void] (Get-RabbitManagementStatus -Method Put -Path ('/api/permissions/{0}/{1}' -f $encodedVHost, $encodedUser) -Body @{
        configure = '.*'
        write = '.*'
        read = '.*'
    })
}

function Enable-RabbitManagement {
    param(
        [Parameter(Mandatory = $true)]
        [string] $RabbitHost
    )

    if (Test-NativeTcpEndpoint -HostName $RabbitHost -Port $RabbitManagementPort) {
        return
    }

    $cookie = (Get-Content -Raw -LiteralPath 'C:\OSPanel\user\.erlang.cookie').Trim()
    $pluginResult = Invoke-NativeCapturedProcess `
        -FilePath 'C:\OSPanel\modules\RabbitMQ-3.13\sbin\rabbitmq-plugins.bat' `
        -ArgumentList @('enable', '--online', 'rabbitmq_management') `
        -WorkingDirectory 'C:\OSPanel\modules\RabbitMQ-3.13\sbin' `
        -TimeoutSeconds 60 `
        -Environment @{
            ERLANG_HOME = 'C:\OSPanel\addons\ErlangOTP-26'
            RABBITMQ_HOME = 'C:\OSPanel\modules\RabbitMQ-3.13'
            RABBITMQ_NODENAME = 'rabbit@' + $env:COMPUTERNAME
            RABBITMQ_CTL_ERL_ARGS = '-setcookie ' + $cookie
        }
    if ($pluginResult.ExitCode -ne 0) {
        throw 'Enabling rabbitmq_management online failed.'
    }
    if (-not (Wait-NativeTcpEndpoint -HostName $RabbitHost -Port $RabbitManagementPort -TimeoutSeconds 30)) {
        throw 'RabbitMQ management did not open port 15672.'
    }
}

function Set-RabbitManagementLoopback {
    param(
        [Parameter(Mandatory = $true)]
        [string] $RabbitHost
    )

    $templates = @(
        'C:\OSPanel\config\RabbitMQ-3.13\default\templates\rabbitmq.conf',
        'C:\OSPanel\modules\RabbitMQ-3.13\ospanel_data\default\templates\rabbitmq.conf'
    )
    foreach ($path in $templates) {
        $lines = New-Object System.Collections.Generic.List[string]
        foreach ($line in @(Get-Content -LiteralPath $path)) { $lines.Add($line) }
        if (-not @($lines | Where-Object { $_ -match '^\s*management\.tcp\.ip\s*=' }).Count) {
            $lines.Add('management.tcp.ip = ' + $RabbitHost)
        }
        if (-not @($lines | Where-Object { $_ -match '^\s*management\.tcp\.port\s*=' }).Count) {
            $lines.Add('management.tcp.port = 15672')
        }
        $temporary = $path + '.plane-local-tmp'
        [System.IO.File]::WriteAllLines($temporary, $lines, (New-Object System.Text.UTF8Encoding($false)))
        $backup = $path + '.plane-local-bak'
        [System.IO.File]::Replace($temporary, $path, $backup)
        [System.IO.File]::Delete($backup)
    }

    $generated = 'C:\OSPanel\modules\RabbitMQ-3.13\etc\rabbitmq.conf'
    $generatedLines = @(Get-Content -LiteralPath $generated)
    $hasGeneratedIp = @($generatedLines | Where-Object { $_ -match '^\s*management\.tcp\.ip\s*=' }).Count -gt 0
    if (-not $hasGeneratedIp) {
        $updated = @($generatedLines) + @('management.tcp.ip = ' + $RabbitHost, 'management.tcp.port = 15672')
        $temporary = $generated + '.plane-local-tmp'
        [System.IO.File]::WriteAllLines($temporary, $updated, (New-Object System.Text.UTF8Encoding($false)))
        $backup = $generated + '.plane-local-bak'
        [System.IO.File]::Replace($temporary, $generated, $backup)
        [System.IO.File]::Delete($backup)
        Restart-NativeOspModule -Context $context -Module 'RabbitMQ-3.13' -Port 5672 -HostName $RabbitHost
    }
}

function Resolve-NativeStorageExecutable {
    if ($StorageExecutable -and (Test-Path -LiteralPath $StorageExecutable -PathType Leaf)) {
        return (Resolve-Path -LiteralPath $StorageExecutable).Path
    }

    $target = Join-Path $context.ToolsRoot 'silo\silo.exe'
    if (Test-Path -LiteralPath $target -PathType Leaf) {
        return $target
    }
    if ($NoStorageDownload) {
        throw 'Silo is required. Set MINIO_EXE or omit -NoStorageDownload.'
    }

    $release = Get-NativeSiloRelease -Tag 'RELEASE.2026-09-16T00-00-00Z'
    $checksum = Resolve-NativeSiloChecksum -Release $release
    $archivePath = Join-Path $context.TempRoot $release.ArchiveName
    Write-NativeStatus -Message ("Downloading verified Silo {0}." -f $release.Tag)
    Invoke-WebRequest -UseBasicParsing -Uri $release.ArchiveUri -OutFile $archivePath
    $actual = (Get-FileHash -LiteralPath $archivePath -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actual -ne $checksum) {
        throw 'Downloaded Silo archive failed SHA256 verification.'
    }

    $staging = Join-Path $context.TempRoot ('silo-extract-' + [Guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $staging -Force | Out-Null
    $tar = Invoke-NativeCapturedProcess `
        -FilePath 'tar.exe' `
        -ArgumentList @('-xzf', $archivePath, '-C', $staging) `
        -WorkingDirectory $context.TempRoot `
        -TimeoutSeconds 120
    if ($tar.ExitCode -ne 0) {
        throw 'Extracting the verified Silo archive failed.'
    }
    $executables = @(Get-ChildItem -LiteralPath $staging -Recurse -File -Filter 'silo.exe')
    if ($executables.Count -ne 1) {
        throw 'The Silo archive did not contain exactly one silo.exe.'
    }

    $targetDirectory = Split-Path -Parent $target
    New-Item -ItemType Directory -Path $targetDirectory -Force | Out-Null
    [System.IO.File]::Copy($executables[0].FullName, $target, $false)
    return $target
}

function Start-PlaneStorage {
    param(
        [Parameter(Mandatory = $true)]
        [string] $Executable
    )

    $owned = Get-NativeOwnedProcess -Context $context -Name 'silo'
    if ($owned -and (Test-NativeHttpEndpoint -Uri 'http://127.0.0.1:9000/minio/health/live')) {
        Write-NativeStatus -Level OK -Message 'Reusing owned Silo process.'
        return
    }
    if (Test-NativeTcpEndpoint -HostName '127.0.0.1' -Port 9000) {
        throw 'Port 9000 is in use by a process not owned by this setup.'
    }

    $values = Read-NativeEnvironmentFile -Path $apiEnvPath
    $dataPath = Join-Path $context.DataRoot 'silo'
    New-Item -ItemType Directory -Path $dataPath -Force | Out-Null
    Remove-NativeStalePid -Context $context -Name 'silo' | Out-Null
    [void] (Start-NativeOwnedProcess `
        -Context $context `
        -Name 'silo' `
        -FilePath $Executable `
        -ArgumentList @('server', $dataPath, '--address', '127.0.0.1:9000', '--console-address', '127.0.0.1:9090') `
        -WorkingDirectory $context.DataRoot `
        -Environment @{
            MINIO_ROOT_USER = $values['AWS_ACCESS_KEY_ID']
            MINIO_ROOT_PASSWORD = $values['AWS_SECRET_ACCESS_KEY']
            MINIO_API_CORS_ALLOW_ORIGIN = $values['CORS_ALLOWED_ORIGINS']
        })
    if (-not (Wait-NativeTcpEndpoint -HostName '127.0.0.1' -Port 9000 -TimeoutSeconds $StartupTimeoutSeconds)) {
        throw 'Silo did not open 127.0.0.1:9000.'
    }
    if (-not (Test-NativeHttpEndpoint -Uri 'http://127.0.0.1:9000/minio/health/live')) {
        throw 'Silo did not pass its health endpoint check.'
    }
    Write-NativeStatus -Level OK -Message 'Silo started on 127.0.0.1:9000.'
}

function Invoke-ProjectUv {
    param(
        [Parameter(Mandatory = $true)]
        [string[]] $Arguments,
        [int] $TimeoutSeconds = 180
    )

    return Invoke-NativeCapturedProcess `
        -FilePath $tools.UvxFile `
        -ArgumentList ($tools.UvExactPrefix + $Arguments) `
        -WorkingDirectory $context.ApiRoot `
        -TimeoutSeconds $TimeoutSeconds `
        -ClearEnvFileVariable `
        -Environment @{ PYTHONPATH = $context.ApiRoot }
}

$tools = Get-NativeToolCommands -Context $context
$pnpmVersion = Invoke-NativeCapturedProcess `
    -FilePath $tools.PnpmFile `
    -ArgumentList ($tools.PnpmPrefix + @('--version')) `
    -WorkingDirectory $context.RepoRoot `
    -TimeoutSeconds 30
if ($pnpmVersion.ExitCode -ne 0 -or $pnpmVersion.StandardOutput.Trim() -ne $tools.PnpmVersion) {
    throw ("Setup requires exact pnpm {0}; corepack did not resolve it." -f $tools.PnpmVersion)
}
$uvVersion = Invoke-NativeCapturedProcess `
    -FilePath $tools.UvxFile `
    -ArgumentList ($tools.UvExactPrefix + @('--version')) `
    -WorkingDirectory $context.ApiRoot `
    -TimeoutSeconds 30 `
    -ClearEnvFileVariable
if ($uvVersion.ExitCode -ne 0 -or $uvVersion.StandardOutput -notmatch ([regex]::Escape($tools.UvVersion))) {
    throw ("Setup requires exact uv {0}." -f $tools.UvVersion)
}

if (-not $SkipDependencies) {
    Write-NativeStatus -Message 'Installing Node dependencies with frozen lockfile.'
    $pnpmInstall = Invoke-NativeCapturedProcess `
        -FilePath $tools.PnpmFile `
        -ArgumentList ($tools.PnpmPrefix + @('install', '--frozen-lockfile')) `
        -WorkingDirectory $context.RepoRoot `
        -TimeoutSeconds 600
    if ($pnpmInstall.ExitCode -ne 0) {
        throw 'Frozen pnpm install failed.'
    }

    Write-NativeStatus -Message 'Syncing the isolated API environment with frozen lockfile.'
    $uvSync = Invoke-ProjectUv -Arguments @('sync', '--frozen') -TimeoutSeconds 600
    if ($uvSync.ExitCode -ne 0) {
        throw 'Frozen uv sync failed.'
    }

    Write-NativeStatus -Message 'Building frontend/shared package dependencies for native dev.'
    $frontendBuild = Invoke-NativeCapturedProcess `
        -FilePath $tools.PnpmFile `
        -ArgumentList ($tools.PnpmPrefix + @(
            'turbo', 'run', 'build',
            '--filter=web', '--filter=admin', '--filter=space', '--filter=live'
        )) `
        -WorkingDirectory $context.RepoRoot `
        -TimeoutSeconds 1200
    if ($frontendBuild.ExitCode -ne 0) {
        throw 'Filtered frontend/shared package build failed.'
    }
}

$values = Read-NativeEnvironmentFile -Path $apiEnvPath
$rabbitManagementHost = $values['RABBITMQ_HOST']
$postgresHost = $values['POSTGRES_HOST']
$postgresPort = $values['POSTGRES_PORT']
$postgresDatabase = $values['POSTGRES_DB']
$postgresUser = $values['POSTGRES_USER']
$postgresPassword = $values['POSTGRES_PASSWORD']

$postgresSetupPath = Join-Path $context.RepoRoot 'dev\ensure_native_postgres.py'
$postgresSetup = Invoke-NativeCapturedProcess `
    -FilePath $tools.UvxFile `
    -ArgumentList ($tools.UvExactPrefix + @('run', '--env-file', '.env', 'python', $postgresSetupPath)) `
    -WorkingDirectory $context.ApiRoot `
    -TimeoutSeconds 60 `
    -ClearEnvFileVariable `
    -Environment @{
        PLANE_PG_HOST = $postgresHost
        PLANE_PG_PORT = $postgresPort
        PLANE_PG_APP_USER = $postgresUser
        PLANE_PG_APP_PASSWORD = $postgresPassword
        PLANE_PG_DATABASE = $postgresDatabase
        PLANE_PG_ADMIN_USER = $PostgresAdminUser
        PLANE_PG_ADMIN_PASSWORD = $PostgresAdminPassword
    }
if ($postgresSetup.ExitCode -ne 0) {
    throw 'Could not create or verify the dedicated Plane PostgreSQL role/database.'
}
Write-NativeStatus -Level OK -Message 'Plane database and dedicated role verified.'

$rabbitUser = 'plane_native'
$rabbitVHost = 'plane-native'
$rabbitPassword = $values['RABBITMQ_PASSWORD']
if ($values['RABBITMQ_USER'] -ne $rabbitUser -or $values['RABBITMQ_VHOST'] -ne $rabbitVHost) {
    $rabbitPassword = New-NativeRandomSecret -ByteCount 36
}
try {
    Set-RabbitManagementLoopback -RabbitHost $rabbitManagementHost
    Enable-RabbitManagement -RabbitHost $rabbitManagementHost
    Ensure-RabbitResources -User $rabbitUser -Password $rabbitPassword -VHost $rabbitVHost
} catch {
    throw ("RabbitMQ management API on port {0} is required to create the isolated vhost/user. {1}" -f $RabbitManagementPort, $_.Exception.Message)
}
[void] (Update-NativeEnvironmentCredentials `
    -Context $context `
    -PostgresUser $postgresUser `
    -PostgresPassword $postgresPassword `
    -PostgresDatabase $postgresDatabase `
    -RabbitUser $rabbitUser `
    -RabbitPassword $rabbitPassword `
    -RabbitVHost $rabbitVHost)
Write-NativeStatus -Level OK -Message 'Dedicated RabbitMQ vhost, user, and permissions provisioned.'

$storage = Resolve-NativeStorageExecutable
Start-PlaneStorage -Executable $storage

$storageSetup = Invoke-NativeCapturedProcess `
    -FilePath $tools.UvxFile `
    -ArgumentList ($tools.UvExactPrefix + @('run', '--env-file', '.env', 'python', (Join-Path $context.RepoRoot 'dev\configure_native_storage.py'))) `
    -WorkingDirectory $context.ApiRoot `
    -TimeoutSeconds 60 `
    -ClearEnvFileVariable
if ($storageSetup.ExitCode -ne 0) {
    throw 'Creating the local S3 bucket/CORS failed.'
}

$migration = Invoke-ProjectUv -Arguments @('run', '--env-file', '.env', 'manage.py', 'migrate', '--settings=plane.settings.local') -TimeoutSeconds 300
if ($migration.ExitCode -ne 0) {
    throw 'Django migrations failed.'
}

$signatureBytes = [Text.Encoding]::UTF8.GetBytes(($env:COMPUTERNAME + '|PlaneNative'))
$sha = [Security.Cryptography.SHA256]::Create()
try {
    $signature = ([BitConverter]::ToString($sha.ComputeHash($signatureBytes))).Replace('-', '').ToLowerInvariant()
} finally {
    $sha.Dispose()
}
$registration = Invoke-ProjectUv -Arguments @('run', '--env-file', '.env', 'manage.py', 'register_instance', $signature, '--settings=plane.settings.local') -TimeoutSeconds 120
if ($registration.ExitCode -ne 0) {
    throw 'Plane instance registration failed.'
}

$telemetry = Invoke-ProjectUv -Arguments @('run', '--env-file', '.env', 'python', (Join-Path $context.RepoRoot 'dev\bootstrap_native.py')) -TimeoutSeconds 60
if ($telemetry.ExitCode -ne 0) {
    throw 'Disabling local telemetry after registration failed.'
}

$configuration = Invoke-ProjectUv -Arguments @('run', '--env-file', '.env', 'manage.py', 'configure_instance', '--settings=plane.settings.local') -TimeoutSeconds 120
if ($configuration.ExitCode -ne 0) {
    throw 'Plane instance configuration failed.'
}

Write-NativeStatus -Level OK -Message 'Native setup completed. No Redis flush or clear_cache command was run.'
[pscustomobject]@{
    EnvFiles = 6
    Database = $postgresDatabase
    DatabaseUser = $postgresUser
    RabbitVHost = $rabbitVHost
    RabbitUser = $rabbitUser
    Storage = 'http://127.0.0.1:9000'
    Telemetry = 'disabled'
} | Format-Table -AutoSize
