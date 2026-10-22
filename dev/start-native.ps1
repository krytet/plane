[CmdletBinding()]
param(
    [int] $StartupTimeoutSeconds = 120,
    [string] $StorageExecutable = $env:MINIO_EXE
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

$moduleRoot = $PSScriptRoot
Import-Module (Join-Path $moduleRoot 'NativeDev.Common.psm1') -Force
Import-Module (Join-Path $moduleRoot 'NativeDev.Environment.psm1') -Force

$context = New-NativeContext -RepoRoot (Get-NativeRepoRoot -StartPath $moduleRoot)
Initialize-NativeContext -Context $context
$validation = Test-NativeEnvironmentConfiguration -Context $context
if (-not $validation.IsValid) {
    $validation.Failures | ForEach-Object { Write-NativeStatus -Level ERROR -Message $_ }
    throw 'Environment validation failed; run dev/setup-native.ps1 first.'
}
[void] (Update-NativeApiEndpointConfiguration -Context $context)

$apiEnvPath = Join-Path $context.ApiRoot '.env'
[void] (Assert-NativeLocalEnvironment -ApiEnvPath $apiEnvPath)
$values = Read-NativeEnvironmentFile -Path $apiEnvPath
$tools = Get-NativeToolCommands -Context $context
$apiPort = Get-NativeApiPort
$apiBaseUrl = Get-NativeApiBaseUrl
$webPort = Get-NativeWebPort
$webBaseUrl = Get-NativeWebBaseUrl
Enable-NativeOspServices -Context $context

function Test-OwnedHttpService {
    param(
        [Parameter(Mandatory = $true)]
        [string] $Name,
        [Parameter(Mandatory = $true)]
        [uri] $Uri,
        [int[]] $ExpectedStatusCodes = @(200)
    )

    $owned = Get-NativeOwnedProcess -Context $context -Name $Name
    return [bool] ($owned -and (Test-NativeHttpEndpoint -Uri $Uri -ExpectedStatusCodes $ExpectedStatusCodes))
}

function Start-OwnedService {
    param(
        [Parameter(Mandatory = $true)]
        [string] $Name,
        [Parameter(Mandatory = $true)]
        [string] $FilePath,
        [Parameter(Mandatory = $true)]
        [string[]] $Arguments,
        [Parameter(Mandatory = $true)]
        [string] $WorkingDirectory,
        [hashtable] $Environment = @{},
        [int] $Port = 0,
        [uri] $HealthUri,
        [int[]] $ExpectedStatusCodes = @(200),
        [string] $ConfigurationTag = ''
    )

    $existingOwned = Get-NativeOwnedProcess -Context $context -Name $Name
    $configurationMatches = Test-NativeOwnedProcessConfiguration `
        -OwnedProcess $existingOwned `
        -ConfigurationTag $ConfigurationTag
    if (
        $existingOwned -and
        $configurationMatches -and
        (-not $HealthUri -or (Test-NativeHttpEndpoint -Uri $HealthUri -ExpectedStatusCodes $ExpectedStatusCodes))
    ) {
        Write-NativeStatus -Level OK -Message ("Reusing owned {0} service." -f $Name)
        return
    }
    if ($existingOwned) {
        Write-NativeStatus -Level WARN -Message ("Restarting owned {0} service because its endpoint configuration changed or became unhealthy." -f $Name)
        [void] (Stop-NativeOwnedProcess -Context $context -Name $Name)
    }
    if ($Port -gt 0 -and (Test-NativeTcpEndpoint -HostName '127.0.0.1' -Port $Port)) {
        throw ("{0} port {1} is already in use by a process this launcher does not own." -f $Name, $Port)
    }

    Remove-NativeStalePid -Context $context -Name $Name | Out-Null
    $record = Start-NativeOwnedProcess `
        -Context $context `
        -Name $Name `
        -FilePath $FilePath `
        -ArgumentList $Arguments `
        -WorkingDirectory $WorkingDirectory `
        -Environment $Environment `
        -ConfigurationTag $ConfigurationTag
    Write-NativeStatus -Message ("Started {0} with PID {1}." -f $Name, $record.pid)

    $deadline = [DateTime]::UtcNow.AddSeconds($StartupTimeoutSeconds)
    do {
        Start-Sleep -Milliseconds 500
        $owned = Get-NativeOwnedProcess -Context $context -Name $Name
        if (-not $owned) {
            throw ("{0} exited during startup. Inspect {1} and {2}." -f $Name, $record.stdout, $record.stderr)
        }
        if ($HealthUri -and (Test-NativeHttpEndpoint -Uri $HealthUri -ExpectedStatusCodes $ExpectedStatusCodes)) {
            Write-NativeStatus -Level OK -Message ("{0} passed its protocol check." -f $Name)
            return
        }
        if (-not $HealthUri -and ([DateTime]::UtcNow.AddSeconds(-5) -lt $deadline)) {
            Write-NativeStatus -Level OK -Message ("{0} is running." -f $Name)
            return
        }
    } while ([DateTime]::UtcNow -lt $deadline)
    throw ("{0} did not become ready within {1} seconds." -f $Name, $StartupTimeoutSeconds)
}

function Ensure-PlaneStorage {
    $executable = $StorageExecutable
    if (-not $executable) {
        $executable = Join-Path $context.ToolsRoot 'silo\silo.exe'
    }
    if (-not (Test-Path -LiteralPath $executable -PathType Leaf)) {
        throw 'Silo is missing; run dev/setup-native.ps1.'
    }

    function Set-PlaneStorageCors {
        $storageSetup = Invoke-NativeCapturedProcess `
            -FilePath $tools.UvxFile `
            -ArgumentList ($tools.UvExactPrefix + @(
                'run', '--env-file', '.env',
                'python', (Join-Path $context.RepoRoot 'dev\configure_native_storage.py')
            )) `
            -WorkingDirectory $context.ApiRoot `
            -TimeoutSeconds 60 `
            -ClearEnvFileVariable
        if ($storageSetup.ExitCode -ne 0) {
            throw 'Silo CORS reconciliation failed.'
        }
    }

    $storageEndpoint = [uri] $values['AWS_S3_ENDPOINT_URL']
    $storageBucket = [string] $values['AWS_S3_BUCKET_NAME']
    $storageCorsOrigins = [string] $values['CORS_ALLOWED_ORIGINS']
    $storageConfigurationTag = 'silo-cors:' + $storageCorsOrigins
    $owned = Get-NativeOwnedProcess -Context $context -Name 'silo'
    $configurationMatches = Test-NativeOwnedProcessConfiguration `
        -OwnedProcess $owned `
        -ConfigurationTag $storageConfigurationTag
    $reuseStorage = $false
    if (
        $owned -and
        $configurationMatches -and
        (Test-NativeHttpEndpoint -Uri 'http://127.0.0.1:9000/minio/health/live')
    ) {
        Write-NativeStatus -Level OK -Message 'Reusing owned Silo process.'
        $reuseStorage = $true
    }
    if (-not $reuseStorage) {
        if ($owned) {
            Write-NativeStatus -Level WARN -Message 'Restarting owned Silo process because its storage CORS configuration changed or became unhealthy.'
            [void] (Stop-NativeOwnedProcess -Context $context -Name 'silo')
        }
        if (Test-NativeTcpEndpoint -HostName '127.0.0.1' -Port 9000) {
            throw 'Port 9000 is in use by a process not owned by this launcher.'
        }

        $dataPath = Join-Path $context.DataRoot 'silo'
        New-Item -ItemType Directory -Path $dataPath -Force | Out-Null
        Remove-NativeStalePid -Context $context -Name 'silo' | Out-Null
        [void] (Start-NativeOwnedProcess `
            -Context $context `
            -Name 'silo' `
            -FilePath $executable `
            -ArgumentList @('server', $dataPath, '--address', '127.0.0.1:9000', '--console-address', '127.0.0.1:9090') `
            -WorkingDirectory $context.DataRoot `
            -Environment @{
                MINIO_ROOT_USER = $values['AWS_ACCESS_KEY_ID']
                MINIO_ROOT_PASSWORD = $values['AWS_SECRET_ACCESS_KEY']
                MINIO_API_CORS_ALLOW_ORIGIN = $storageCorsOrigins
            } `
            -ConfigurationTag $storageConfigurationTag)
        if (-not (Wait-NativeTcpEndpoint -HostName '127.0.0.1' -Port 9000 -TimeoutSeconds $StartupTimeoutSeconds)) {
            throw 'Silo did not open 127.0.0.1:9000.'
        }
    }

    $deadline = [DateTime]::UtcNow.AddSeconds($StartupTimeoutSeconds)
    $corsConfigurationAttempted = $false
    do {
        $webOriginAllowed = Test-NativeStorageCorsOrigin `
            -Endpoint $storageEndpoint `
            -Bucket $storageBucket `
            -Origin $webBaseUrl
        $legacyOriginRejected = -not (Test-NativeStorageCorsOrigin `
            -Endpoint $storageEndpoint `
            -Bucket $storageBucket `
            -Origin 'http://localhost:3000')
        if ($webOriginAllowed -and $legacyOriginRejected) {
            Write-NativeStatus -Level OK -Message 'Silo accepts CORS from the native web origin and rejects the legacy 3000 origin.'
            return
        }
        if (-not $corsConfigurationAttempted) {
            Write-NativeStatus -Level WARN -Message 'Reconciling owned Silo CORS configuration without changing storage data.'
            Set-PlaneStorageCors
            $corsConfigurationAttempted = $true
            continue
        }
        Start-Sleep -Milliseconds 500
    } while ([DateTime]::UtcNow -lt $deadline)
    throw 'Silo CORS readiness failed: the native web origin is not allowed or the legacy 3000 origin is still accepted.'
}

$psql = 'C:\OSPanel\modules\PostgreSQL-15\bin\psql.exe'
$dbCheck = Invoke-NativeCapturedProcess `
    -FilePath $psql `
    -ArgumentList @('-h', $values['POSTGRES_HOST'], '-p', $values['POSTGRES_PORT'], '-U', $values['POSTGRES_USER'], '-d', $values['POSTGRES_DB'], '-tAc', 'SELECT 1;') `
    -WorkingDirectory (Split-Path -Parent $psql) `
    -TimeoutSeconds 15 `
    -Environment @{ PGPASSWORD = $values['POSTGRES_PASSWORD'] }
if ($dbCheck.ExitCode -ne 0 -or $dbCheck.StandardOutput.Trim() -ne '1') {
    throw 'PostgreSQL protocol check failed.'
}

$redisCli = 'C:\OSPanel\modules\Redis\redis-cli.exe'
$redisDatabase = ([Uri] $values['REDIS_URL']).AbsolutePath.Trim('/')
if (-not $redisDatabase) {
    $redisDatabase = '0'
}
$redisCheck = Invoke-NativeCapturedProcess `
    -FilePath $redisCli `
    -ArgumentList @('-h', $values['REDIS_HOST'], '-p', $values['REDIS_PORT'], '-n', $redisDatabase, 'ping') `
    -WorkingDirectory (Split-Path -Parent $redisCli) `
    -TimeoutSeconds 10
if ($redisCheck.ExitCode -ne 0 -or $redisCheck.StandardOutput.Trim() -ne 'PONG') {
    throw 'Redis protocol check failed.'
}

$python = Join-Path $context.ApiRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw 'The isolated API environment is missing; run dev/setup-native.ps1.'
}
$rabbitCode = 'import os; from kombu import Connection; c=Connection(os.environ["PLANE_AMQP_URL"], connect_timeout=5); c.ensure_connection(max_retries=1); c.release(); print("AMQP_OK")'
$rabbitCheck = Invoke-NativeCapturedProcess `
    -FilePath $python `
    -ArgumentList @('-c', $rabbitCode) `
    -WorkingDirectory $context.ApiRoot `
    -TimeoutSeconds 15 `
    -Environment @{ PLANE_AMQP_URL = $values['AMQP_URL'] }
if ($rabbitCheck.ExitCode -ne 0 -or $rabbitCheck.StandardOutput -notmatch 'AMQP_OK') {
    throw 'RabbitMQ AMQP check failed for the isolated vhost.'
}
Ensure-PlaneStorage
$healthUri = [uri] ($values['AWS_S3_ENDPOINT_URL'].TrimEnd('/') + '/minio/health/live')
if (-not (Test-NativeHttpEndpoint -Uri $healthUri)) {
    throw 'Local S3 storage is not healthy.'
}

$apiArguments = $tools.UvExactPrefix + @(
    'run', '--env-file', '.env',
    'manage.py', 'runserver', ('127.0.0.1:{0}' -f $apiPort),
    '--settings=plane.settings.local'
)
Start-OwnedService `
    -Name 'api' `
    -FilePath $tools.UvxFile `
    -Arguments $apiArguments `
    -WorkingDirectory $context.ApiRoot `
    -Port $apiPort `
    -HealthUri ($apiBaseUrl + '/') `
    -ConfigurationTag ('native-api:{0}:web:{1}' -f $apiBaseUrl, $webBaseUrl)

$chatSyncModulePath = Join-Path $context.RepoRoot 'dev\chat-sync\NativeChatSync.psm1'
if (Test-Path -LiteralPath $chatSyncModulePath -PathType Leaf) {
    Import-Module $chatSyncModulePath -Force
    $chatSyncStart = Start-NativeChatSyncService `
        -Context $context `
        -UvxFile $tools.UvxFile `
        -UvExactPrefix $tools.UvExactPrefix `
        -ApiBaseUrl $apiBaseUrl `
        -StartupTimeoutSeconds $StartupTimeoutSeconds
    if ($chatSyncStart.State -in @('started', 'reused')) {
        Write-NativeStatus `
            -Level OK `
            -Message ('chat-sync {0} with PID {1}.' -f $chatSyncStart.State, $chatSyncStart.OwnedProcess.Record.pid)
    }
}

$djangoEnvironment = @{ DJANGO_SETTINGS_MODULE = 'plane.settings.local' }
Start-OwnedService `
    -Name 'worker' `
    -FilePath $tools.UvxFile `
    -Arguments ($tools.UvExactPrefix + @('run', '--env-file', '.env', 'celery', '-A', 'plane', 'worker', '-l', 'info', '--pool=solo', '--concurrency=1')) `
    -WorkingDirectory $context.ApiRoot `
    -Environment $djangoEnvironment `
    -ConfigurationTag ('native-backend:api:{0}:web:{1}' -f $apiBaseUrl, $webBaseUrl)

$celeryPingPath = Join-Path $context.RepoRoot 'dev\check_celery_worker.py'
$deadline = [DateTime]::UtcNow.AddSeconds($StartupTimeoutSeconds)
$workerReady = $false
do {
    $ping = Invoke-NativeCapturedProcess `
        -FilePath $tools.UvxFile `
        -ArgumentList ($tools.UvExactPrefix + @('run', '--env-file', '.env', 'python', $celeryPingPath)) `
        -WorkingDirectory $context.ApiRoot `
        -TimeoutSeconds 15 `
        -ClearEnvFileVariable `
        -Environment @{
            DJANGO_SETTINGS_MODULE = 'plane.settings.local'
            PYTHONPATH = $context.ApiRoot
        }
    if ($ping.ExitCode -eq 0 -and $ping.StandardOutput -match 'CELERY_PONG') {
        $workerReady = $true
        break
    }
    Start-Sleep -Milliseconds 750
} while ([DateTime]::UtcNow -lt $deadline)
if (-not $workerReady) {
    throw 'Celery worker did not answer inspect ping.'
}
Write-NativeStatus -Level OK -Message 'Celery worker answered inspect ping.'

Start-OwnedService `
    -Name 'beat' `
    -FilePath $tools.UvxFile `
    -Arguments ($tools.UvExactPrefix + @('run', '--env-file', '.env', 'celery', '-A', 'plane', 'beat', '-l', 'info')) `
    -WorkingDirectory $context.ApiRoot `
    -Environment $djangoEnvironment `
    -ConfigurationTag ('native-backend:api:{0}:web:{1}' -f $apiBaseUrl, $webBaseUrl)

$webServices = @(
    @{ Name = 'web'; Package = 'web'; Port = $webPort },
    @{ Name = 'admin'; Package = 'admin'; Port = 3001 },
    @{ Name = 'space'; Package = 'space'; Port = 3002 }
)
foreach ($service in $webServices) {
    $healthPath = if ($service.Name -eq 'admin') { '/god-mode/' } elseif ($service.Name -eq 'space') { '/spaces/' } else { '/' }
    if ($service.Name -eq 'web') {
        $webEnvironmentPath = Join-Path $context.RepoRoot 'apps\web\.env'
        $webEnvironmentValues = Read-NativeEnvironmentFile -Path $webEnvironmentPath
        $webApiBaseUrl = [string] $webEnvironmentValues['VITE_API_BASE_URL']
        $webInstanceReady = Test-NativeWebInstanceReadiness -ApiBaseUrl $webApiBaseUrl
        if ((Get-NativeOwnedProcess -Context $context -Name 'web') -and -not $webInstanceReady.Ok) {
            [void] (Stop-NativeOwnedProcess -Context $context -Name 'web')
        }
    }
    $serviceEnvironment = if ($service.Name -eq 'web') {
        @{ NATIVE_API_PROXY_TARGET = $apiBaseUrl }
    } else {
        @{}
    }
    $devArguments = $tools.PnpmPrefix + @(
        '--filter', $service.Package,
        'exec', 'react-router', 'dev',
        '--host', '127.0.0.1',
        '--port', [string] $service.Port
    )
    if ($service.Name -eq 'web') {
        $devArguments += '--strictPort'
    }
    $serviceConfigurationTag = switch ($service.Name) {
        'web' { 'native-web:{0}:api:{1}' -f $webBaseUrl, $apiBaseUrl }
        'admin' { 'native-browser-api:{0}' -f $webBaseUrl }
        'space' { 'native-browser-api:{0}' -f $webBaseUrl }
    }
    Start-OwnedService `
        -Name $service.Name `
        -FilePath $tools.PnpmFile `
        -Arguments $devArguments `
        -WorkingDirectory $context.RepoRoot `
        -Environment $serviceEnvironment `
        -Port $service.Port `
        -HealthUri ("http://127.0.0.1:{0}{1}" -f $service.Port, $healthPath) `
        -ConfigurationTag $serviceConfigurationTag
    if ($service.Name -eq 'web') {
        $webInstanceReady = Test-NativeWebInstanceReadiness -ApiBaseUrl $webApiBaseUrl
        if (-not $webInstanceReady.Ok) {
            throw ("web API proxy readiness failed: {0}" -f $webInstanceReady.Reason)
        }
    }
}

Start-OwnedService `
    -Name 'live' `
    -FilePath $tools.PnpmFile `
    -Arguments ($tools.PnpmPrefix + @('--filter', 'live', 'dev')) `
    -WorkingDirectory $context.RepoRoot `
    -Environment @{ API_BASE_URL = $apiBaseUrl } `
    -Port 3100 `
    -HealthUri 'http://127.0.0.1:3100/live/health' `
    -ConfigurationTag ('native-live:api:{0}:web:{1}' -f $apiBaseUrl, $webBaseUrl)

Write-NativeStatus -Level OK -Message 'Plane native services are running on loopback.'
[pscustomobject]@{
    Web = $webBaseUrl
    Admin = 'http://localhost:3001/god-mode'
    Space = 'http://localhost:3002/spaces'
    API = $apiBaseUrl
    Live = 'http://localhost:3100/live'
    Storage = 'http://127.0.0.1:9000'
} | Format-Table -AutoSize
