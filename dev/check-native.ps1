[CmdletBinding()]
param(
    [int] $StartupTimeoutSeconds = 5,
    [Alias('InfraOnly')]
    [switch] $InfrastructureOnly,
    [switch] $RequireSetupDone
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

$moduleRoot = $PSScriptRoot
Import-Module (Join-Path $moduleRoot 'NativeDev.Common.psm1') -Force
Import-Module (Join-Path $moduleRoot 'NativeDev.Environment.psm1') -Force

$context = New-NativeContext -RepoRoot (Get-NativeRepoRoot -StartPath $moduleRoot)
$validation = Test-NativeEnvironmentConfiguration -Context $context
$checks = @()

foreach ($file in @(Get-NativeEnvironmentFiles -Context $context)) {
    $checks += [pscustomobject]@{
        Check = 'env:' + $file.Name
        Ok = (Test-Path -LiteralPath $file.Path -PathType Leaf)
        Detail = $file.Path
    }
}
$checks += [pscustomobject]@{ Check = 'env:required-keys'; Ok = $validation.IsValid; Detail = ($validation.Failures -join '; ') }
if (-not $validation.IsValid) {
    $checks | Format-Table -Wrap -AutoSize
    exit 1
}

$apiEnvPath = Join-Path $context.ApiRoot '.env'
$targets = Assert-NativeLocalEnvironment -ApiEnvPath $apiEnvPath
$values = Read-NativeEnvironmentFile -Path $apiEnvPath
foreach ($target in $targets) {
    $checks += [pscustomobject]@{
        Check = 'loopback:' + $target.Name
        Ok = $true
        Detail = ('{0}:{1}' -f $target.Host, $target.Port)
    }
}

$tools = Get-NativeToolCommands -Context $context
$nativeApiBaseUrl = Get-NativeApiBaseUrl
$nativeWebBaseUrl = Get-NativeWebBaseUrl
$rootEndpointValues = Read-NativeEnvironmentFile -Path (Join-Path $context.RepoRoot '.env')
$webEndpointValues = Read-NativeEnvironmentFile -Path (Join-Path $context.RepoRoot 'apps\web\.env')
$adminEndpointValues = Read-NativeEnvironmentFile -Path (Join-Path $context.RepoRoot 'apps\admin\.env')
$spaceEndpointValues = Read-NativeEnvironmentFile -Path (Join-Path $context.RepoRoot 'apps\space\.env')
$liveEndpointValues = Read-NativeEnvironmentFile -Path (Join-Path $context.RepoRoot 'apps\live\.env')
$webProxyTarget = [string] $webEndpointValues['NATIVE_API_PROXY_TARGET']
$liveApiTarget = [string] $liveEndpointValues['API_BASE_URL']
$targetChecks = @(
    @{ Name = 'web-proxy'; Actual = $webProxyTarget; Expected = $nativeApiBaseUrl; Key = 'NATIVE_API_PROXY_TARGET' },
    @{ Name = 'live-api'; Actual = $liveApiTarget; Expected = $nativeApiBaseUrl; Key = 'API_BASE_URL' },
    @{ Name = 'web-api-base'; Actual = [string] $webEndpointValues['VITE_API_BASE_URL']; Expected = $nativeWebBaseUrl; Key = 'web VITE_API_BASE_URL' },
    @{ Name = 'web-base-url'; Actual = [string] $webEndpointValues['VITE_WEB_BASE_URL']; Expected = $nativeWebBaseUrl; Key = 'web VITE_WEB_BASE_URL' },
    @{ Name = 'admin-api-base'; Actual = [string] $adminEndpointValues['VITE_API_BASE_URL']; Expected = $nativeWebBaseUrl; Key = 'admin VITE_API_BASE_URL' },
    @{ Name = 'admin-web-base'; Actual = [string] $adminEndpointValues['VITE_WEB_BASE_URL']; Expected = $nativeWebBaseUrl; Key = 'admin VITE_WEB_BASE_URL' },
    @{ Name = 'space-api-base'; Actual = [string] $spaceEndpointValues['VITE_API_BASE_URL']; Expected = $nativeWebBaseUrl; Key = 'space VITE_API_BASE_URL' },
    @{ Name = 'space-web-base'; Actual = [string] $spaceEndpointValues['VITE_WEB_BASE_URL']; Expected = $nativeWebBaseUrl; Key = 'space VITE_WEB_BASE_URL' },
    @{ Name = 'api-web-url'; Actual = [string] $values['WEB_URL']; Expected = $nativeWebBaseUrl; Key = 'API WEB_URL' },
    @{ Name = 'api-app-base'; Actual = [string] $values['APP_BASE_URL']; Expected = $nativeWebBaseUrl; Key = 'API APP_BASE_URL' }
)
foreach ($targetCheck in $targetChecks) {
    $checks += [pscustomobject]@{
        Check = 'target:' + $targetCheck.Name
        Ok = (Test-NativeApiEndpointTarget -Actual $targetCheck.Actual -Expected $targetCheck.Expected)
        Detail = if ($targetCheck.Actual) {
            $targetCheck.Actual
        } else {
            'missing ' + $targetCheck.Key
        }
    }
}
foreach ($originCheck in @(
    @{ Name = 'root-cors'; Value = [string] $rootEndpointValues['CORS_ALLOWED_ORIGINS'] },
    @{ Name = 'api-cors'; Value = [string] $values['CORS_ALLOWED_ORIGINS'] },
    @{ Name = 'live-cors'; Value = [string] $liveEndpointValues['CORS_ALLOWED_ORIGINS'] }
)) {
    $origins = @($originCheck.Value -split ',' | ForEach-Object { $_.Trim() })
    $containsNativeWebOrigin = $origins -contains $nativeWebBaseUrl
    $containsLegacyOrigin = @($origins | Where-Object {
        $_ -in @('http://localhost:3000', 'http://127.0.0.1:3000')
    }).Count -gt 0
    $checks += [pscustomobject]@{
        Check = 'target:' + $originCheck.Name
        Ok = ($containsNativeWebOrigin -and -not $containsLegacyOrigin)
        Detail = $originCheck.Value
    }
}
$storageEndpoint = [uri] $values['AWS_S3_ENDPOINT_URL']
$storageBucket = [string] $values['AWS_S3_BUCKET_NAME']
$storageNativeAllowed = Test-NativeStorageCorsOrigin `
    -Endpoint $storageEndpoint `
    -Bucket $storageBucket `
    -Origin $nativeWebBaseUrl
$storageLocalhost3000Rejected = -not (Test-NativeStorageCorsOrigin `
    -Endpoint $storageEndpoint `
    -Bucket $storageBucket `
    -Origin 'http://localhost:3000')
$storageIpv4_3000Rejected = -not (Test-NativeStorageCorsOrigin `
    -Endpoint $storageEndpoint `
    -Bucket $storageBucket `
    -Origin 'http://127.0.0.1:3000')
$checks += [pscustomobject]@{
    Check = 'storage:cors-policy'
    Ok = ($storageNativeAllowed -and $storageLocalhost3000Rejected -and $storageIpv4_3000Rejected)
    Detail = '3059 allowed; legacy 3000 origins rejected'
}
$pnpm = Invoke-NativeCapturedProcess -FilePath $tools.PnpmFile -ArgumentList ($tools.PnpmPrefix + @('--version')) -WorkingDirectory $context.RepoRoot -TimeoutSeconds 30
$checks += [pscustomobject]@{ Check = 'tool:pnpm'; Ok = ($pnpm.ExitCode -eq 0 -and $pnpm.StandardOutput.Trim() -eq '11.10.0'); Detail = $pnpm.StandardOutput.Trim() }
$uv = Invoke-NativeCapturedProcess -FilePath $tools.UvxFile -ArgumentList ($tools.UvExactPrefix + @('--version')) -WorkingDirectory $context.ApiRoot -TimeoutSeconds 30 -ClearEnvFileVariable
$checks += [pscustomobject]@{ Check = 'tool:uv'; Ok = ($uv.ExitCode -eq 0 -and $uv.StandardOutput -match '0\.12\.21'); Detail = $uv.StandardOutput.Trim() }
$checks += [pscustomobject]@{ Check = 'api:venv'; Ok = (Test-Path -LiteralPath (Join-Path $context.ApiRoot '.venv\Scripts\python.exe') -PathType Leaf); Detail = '.venv\Scripts\python.exe' }

$checks += [pscustomobject]@{ Check = 'postgres:tcp'; Ok = (Test-NativeTcpEndpoint -HostName $values['POSTGRES_HOST'] -Port ([int] $values['POSTGRES_PORT'])); Detail = ('{0}:{1}' -f $values['POSTGRES_HOST'], $values['POSTGRES_PORT']) }
$checks += [pscustomobject]@{ Check = 'redis:tcp'; Ok = (Test-NativeTcpEndpoint -HostName $values['REDIS_HOST'] -Port ([int] $values['REDIS_PORT'])); Detail = ('{0}:{1}' -f $values['REDIS_HOST'], $values['REDIS_PORT']) }
$checks += [pscustomobject]@{ Check = 'rabbit:tcp'; Ok = (Test-NativeTcpEndpoint -HostName $values['RABBITMQ_HOST'] -Port ([int] $values['RABBITMQ_PORT'])); Detail = ('{0}:{1}' -f $values['RABBITMQ_HOST'], $values['RABBITMQ_PORT']) }
$managementListeners = @(Get-NetTCPConnection -State Listen -LocalPort 15672 -ErrorAction SilentlyContinue)
$managementLoopback = $managementListeners.Count -gt 0 -and @($managementListeners | Where-Object {
    $_.LocalAddress -notin @('127.0.1.22', '::1', '127.0.0.1')
}).Count -eq 0
$checks += [pscustomobject]@{ Check = 'rabbit:management-loopback'; Ok = $managementLoopback; Detail = if ($managementListeners.Count) { ($managementListeners.LocalAddress -join ',') + ':15672' } else { 'not listening' } }
$s3Health = [uri] ($values['AWS_S3_ENDPOINT_URL'].TrimEnd('/') + '/minio/health/live')
$checks += [pscustomobject]@{ Check = 's3:health'; Ok = (Test-NativeHttpEndpoint -Uri $s3Health); Detail = $values['AWS_S3_ENDPOINT_URL'] }

$ports = @(
    @{ Name = 'api'; Port = (Get-NativeApiPort) },
    @{ Name = 'web'; Port = (Get-NativeWebPort) },
    @{ Name = 'admin'; Port = 3001 },
    @{ Name = 'space'; Port = 3002 },
    @{ Name = 'live'; Port = 3100 }
)
foreach ($service in $ports) {
    $owned = Get-NativeOwnedProcess -Context $context -Name $service.Name
    $listening = Test-NativeTcpEndpoint -HostName '127.0.0.1' -Port $service.Port
    $detail = 'not running'
    if ($listening -and $owned) {
        $detail = 'listening; owned PID ' + $owned.Record.pid
    } elseif ($listening) {
        $detail = 'listening; ownership unknown'
    }
    $checks += [pscustomobject]@{
        Check = ('app:' + $service.Name)
        Ok = ($listening -and $null -ne $owned)
        Detail = $detail
    }
}

if (-not $InfrastructureOnly) {
    $webValues = Read-NativeEnvironmentFile -Path (Join-Path $context.RepoRoot 'apps\web\.env')
    $browserApiBaseUrl = [string] $webValues['VITE_API_BASE_URL']
    $instanceReadiness = if ($browserApiBaseUrl) {
        Test-NativeWebInstanceReadiness `
            -ApiBaseUrl $browserApiBaseUrl `
            -Origin $nativeWebBaseUrl `
            -RequireSetupDone:$RequireSetupDone
    } else {
        [pscustomobject]@{ Ok = $false; Uri = ''; Reason = 'missing-vite-api-base-url' }
    }
    $checks += [pscustomobject]@{
        Check = 'instance:web-api'
        Ok = $instanceReadiness.Ok
        Detail = if ($instanceReadiness.Ok) {
            '{0}; setup={1}; activated={2}; self_managed={3}; cors={4}' -f `
                $instanceReadiness.Uri,
                $instanceReadiness.SetupDone,
                $instanceReadiness.Activated,
                $instanceReadiness.SelfManaged,
                $instanceReadiness.Cors
        } else {
            '{0}; {1}' -f $instanceReadiness.Uri, $instanceReadiness.Reason
        }
    }

    $httpChecks = @(
        @{ Name = 'api'; Uri = ($nativeApiBaseUrl + '/') },
        @{ Name = 'web'; Uri = ('http://127.0.0.1:{0}/' -f (Get-NativeWebPort)) },
        @{ Name = 'admin'; Uri = 'http://127.0.0.1:3001/god-mode/' },
        @{ Name = 'space'; Uri = 'http://127.0.0.1:3002/spaces/' },
        @{ Name = 'live'; Uri = 'http://127.0.0.1:3100/live/health' }
    )
    foreach ($httpCheck in $httpChecks) {
        $checks += [pscustomobject]@{
            Check = ('http:' + $httpCheck.Name)
            Ok = (Test-NativeHttpEndpoint -Uri ([uri] $httpCheck.Uri))
            Detail = $httpCheck.Uri
        }
    }
    foreach ($workerName in @('worker', 'beat')) {
        $owned = Get-NativeOwnedProcess -Context $context -Name $workerName
        $checks += [pscustomobject]@{
            Check = ('process:' + $workerName)
            Ok = ($null -ne $owned)
            Detail = if ($owned) { 'owned PID ' + $owned.Record.pid } else { 'not running' }
        }
    }
    $chatSyncModulePath = Join-Path $context.RepoRoot 'dev\chat-sync\NativeChatSync.psm1'
    if (Test-Path -LiteralPath $chatSyncModulePath -PathType Leaf) {
        try {
            Import-Module $chatSyncModulePath -Force
            $chatSyncConfig = Get-NativeChatSyncServiceConfig -RepoRoot $context.RepoRoot
            if (-not $chatSyncConfig.Enabled) {
                $checks += [pscustomobject]@{
                    Check = 'app:chat-sync'
                    Ok = $true
                    Detail = 'disabled'
                }
            } else {
                $chatSyncStatus = Get-NativeChatSyncStatus `
                    -Context $context `
                    -Config $chatSyncConfig `
                    -ApiBaseUrl $nativeApiBaseUrl
                $chatSyncOwned = $chatSyncStatus.OwnedProcess
                $checks += [pscustomobject]@{
                    Check = 'process:chat-sync'
                    Ok = ($null -ne $chatSyncOwned)
                    Detail = if ($chatSyncOwned) {
                        'owned PID ' + $chatSyncOwned.Record.pid
                    } else {
                        'not running'
                    }
                }
                $checks += [pscustomobject]@{
                    Check = 'app:chat-sync-heartbeat'
                    Ok = $chatSyncStatus.Ok
                    Detail = if ($chatSyncStatus.Ok) {
                        'fresh cycle; age={0:N0}s; workspace={1}; threads={2}' -f `
                            $chatSyncStatus.AgeSeconds,
                            $chatSyncStatus.Heartbeat.workspace,
                            $chatSyncStatus.ThreadCount
                    } else {
                        $chatSyncStatus.Reason
                    }
                }
            }
        } catch {
            $checks += [pscustomobject]@{
                Check = 'app:chat-sync-heartbeat'
                Ok = $false
                Detail = 'invalid service configuration or status'
            }
        }
    }
    $pingScript = Join-Path $context.RepoRoot 'dev\check_celery_worker.py'
    $ping = Invoke-NativeCapturedProcess `
        -FilePath $tools.UvxFile `
        -ArgumentList ($tools.UvExactPrefix + @('run', '--env-file', '.env', 'python', $pingScript)) `
        -WorkingDirectory $context.ApiRoot `
        -TimeoutSeconds 15 `
        -ClearEnvFileVariable `
        -Environment @{
            DJANGO_SETTINGS_MODULE = 'plane.settings.local'
            PYTHONPATH = $context.ApiRoot
        }
    $checks += [pscustomobject]@{
        Check = 'worker:inspect-ping'
        Ok = ($ping.ExitCode -eq 0 -and $ping.StandardOutput -match 'CELERY_PONG')
        Detail = if ($ping.ExitCode -eq 0) { 'reply received' } else { 'no reply' }
    }
}

$checks | Format-Table -Wrap -AutoSize
$requiredChecks = @($checks | Where-Object {
    -not $_.Ok -and (-not $InfrastructureOnly -or $_.Check -notlike 'app:*')
})
if ($requiredChecks.Count -gt 0) {
    exit 1
}
exit 0
