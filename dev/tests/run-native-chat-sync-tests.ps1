[CmdletBinding()]
param()

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

Import-Module (Join-Path $PSScriptRoot '..\NativeDev.Common.psm1') -Force
Import-Module (Join-Path $PSScriptRoot '..\chat-sync\NativeChatSync.psm1') -Force

$script:Passed = 0
$script:Failed = 0
$tempRoot = Join-Path ([System.IO.Path]::GetTempPath()) (
    'plane-chat-sync-tests-' + [Guid]::NewGuid().ToString('N')
)
$repoRoot = Join-Path $tempRoot 'repo'
$configPath = Join-Path $tempRoot 'service.json'
$stateDir = Join-Path $tempRoot 'state'
$encoding = New-Object System.Text.UTF8Encoding($false)

function Invoke-TestCase {
    param(
        [Parameter(Mandatory = $true)]
        [string] $Name,
        [Parameter(Mandatory = $true)]
        [scriptblock] $Test
    )

    try {
        & $Test
        $script:Passed++
        Write-Host ("PASS {0}" -f $Name)
    } catch {
        $script:Failed++
        Write-Host ("FAIL {0}: {1}" -f $Name, $_.Exception.Message)
    }
}

function Assert-Equal {
    param($Actual, $Expected)
    if ($Actual -ne $Expected) {
        throw ("Expected '{0}', got '{1}'." -f $Expected, $Actual)
    }
}

function Assert-True {
    param([bool] $Condition, [string] $Message = 'Expected the condition to be true.')
    if (-not $Condition) {
        throw $Message
    }
}

function Assert-Throws {
    param([scriptblock] $Action)
    try {
        & $Action
    } catch {
        return
    }
    throw 'Expected the action to throw.'
}

function Write-JsonFile {
    param(
        [Parameter(Mandatory = $true)]
        [string] $Path,
        [Parameter(Mandatory = $true)]
        [object] $Value
    )

    [System.IO.File]::WriteAllText(
        $Path,
        (($Value | ConvertTo-Json -Depth 5 -Compress) + "`n"),
        $encoding
    )
}

function New-ServiceConfig {
    param([bool] $Enabled = $true)
    return [ordered]@{
        enabled = $Enabled
        workspace = 'personal'
        codexExecutable = 'C:\fake\codex.exe'
        pollIntervalSeconds = 30
        stateDir = $stateDir
    }
}

New-Item -ItemType Directory -Path $repoRoot -Force | Out-Null
New-Item -ItemType Directory -Path $stateDir -Force | Out-Null

try {
    Invoke-TestCase -Name 'valid service config uses the strict external whitelist' -Test {
        Write-JsonFile -Path $configPath -Value (New-ServiceConfig)
        $config = Get-NativeChatSyncServiceConfig -ConfigPath $configPath -RepoRoot $repoRoot
        Assert-True -Condition $config.Enabled
        Assert-Equal -Actual $config.Workspace -Expected 'personal'
        Assert-Equal -Actual $config.PollIntervalSeconds -Expected 30
        Assert-Equal -Actual $config.StateDir -Expected $stateDir
        Assert-True -Condition ($config.ConfigHash -match '^[0-9a-f]{64}$')
        $tag = Get-NativeChatSyncConfigurationTag -Config $config -ApiBaseUrl 'http://127.0.0.1:8059'
        Assert-True -Condition ($tag -like ("native-chat-sync:{0}:api:http://127.0.0.1:8059" -f $config.ConfigHash))
    }

    Invoke-TestCase -Name 'secret and unknown fields fail closed without being accepted' -Test {
        $payload = New-ServiceConfig
        $payload.apiKey = 'must-not-be-accepted'
        Write-JsonFile -Path $configPath -Value $payload
        Assert-Throws { Get-NativeChatSyncServiceConfig -ConfigPath $configPath -RepoRoot $repoRoot }

        $payload = New-ServiceConfig
        $payload.planeBaseUrl = 'http://127.0.0.1:8059'
        Write-JsonFile -Path $configPath -Value $payload
        Assert-Throws { Get-NativeChatSyncServiceConfig -ConfigPath $configPath -RepoRoot $repoRoot }
    }

    Invoke-TestCase -Name 'service config and state directory must remain outside the repository' -Test {
        $insideRepoConfig = Join-Path $repoRoot 'service.json'
        Write-JsonFile -Path $insideRepoConfig -Value (New-ServiceConfig)
        Assert-Throws { Get-NativeChatSyncServiceConfig -ConfigPath $insideRepoConfig -RepoRoot $repoRoot }

        $payload = New-ServiceConfig
        $payload.stateDir = Join-Path $repoRoot 'state'
        Write-JsonFile -Path $configPath -Value $payload
        Assert-Throws { Get-NativeChatSyncServiceConfig -ConfigPath $configPath -RepoRoot $repoRoot }
    }

    Invoke-TestCase -Name 'disabled or absent config starts no process and has no filesystem side effects' -Test {
        $disabledPath = Join-Path $tempRoot 'disabled.json'
        Write-JsonFile -Path $disabledPath -Value ([ordered]@{ enabled = $false })
        $pidsRoot = Join-Path $tempRoot 'empty-pids'
        $logsRoot = Join-Path $tempRoot 'empty-logs'
        New-Item -ItemType Directory -Path $pidsRoot -Force | Out-Null
        New-Item -ItemType Directory -Path $logsRoot -Force | Out-Null
        $context = [pscustomobject]@{
            RepoRoot = $repoRoot
            ApiRoot = $repoRoot
            PidsRoot = $pidsRoot
            LogsRoot = $logsRoot
        }

        $result = Start-NativeChatSyncService `
            -Context $context `
            -UvxFile 'unused-uvx.exe' `
            -UvExactPrefix @('--from', 'uv==0.12.21', 'uv') `
            -ApiBaseUrl 'http://127.0.0.1:8059' `
            -ConfigPath $disabledPath
        Assert-Equal -Actual $result.State -Expected 'disabled'
        Assert-Equal -Actual @(Get-ChildItem -LiteralPath $pidsRoot -Force).Count -Expected 0
        Assert-Equal -Actual @(Get-ChildItem -LiteralPath $logsRoot -Force).Count -Expected 0
    }

    Invoke-TestCase -Name 'external live status fails without starting or stopping a process' -Test {
        $externalState = Join-Path $tempRoot 'external-state'
        $externalPids = Join-Path $tempRoot 'external-pids'
        $externalLogs = Join-Path $tempRoot 'external-logs'
        New-Item -ItemType Directory -Path $externalState -Force | Out-Null
        New-Item -ItemType Directory -Path $externalPids -Force | Out-Null
        New-Item -ItemType Directory -Path $externalLogs -Force | Out-Null
        $payload = New-ServiceConfig
        $payload.stateDir = $externalState
        $externalConfigPath = Join-Path $tempRoot 'external.json'
        Write-JsonFile -Path $externalConfigPath -Value $payload
        Write-JsonFile -Path (Join-Path $externalState 'status.json') -Value ([ordered]@{
            pid = $PID
            updated_at = [DateTimeOffset]::UtcNow.ToString('o')
            workspace = 'personal'
            intervalSeconds = 30
            exitCode = 0
            threadCount = 0
        })
        $context = [pscustomobject]@{
            RepoRoot = $repoRoot
            ApiRoot = $repoRoot
            PidsRoot = $externalPids
            LogsRoot = $externalLogs
        }

        Assert-Throws {
            Start-NativeChatSyncService `
                -Context $context `
                -UvxFile 'unused-uvx.exe' `
                -UvExactPrefix @('--from', 'uv==0.12.21', 'uv') `
                -ApiBaseUrl 'http://127.0.0.1:8059' `
                -ConfigPath $externalConfigPath
        }
        Assert-Equal -Actual @(Get-ChildItem -LiteralPath $externalPids -Force).Count -Expected 0
        Assert-Equal -Actual @(Get-ChildItem -LiteralPath $externalLogs -Force).Count -Expected 0
    }

    Invoke-TestCase -Name 'chat-sync integration never invokes native process termination and passes service config' -Test {
        $source = [System.IO.File]::ReadAllText(
            (Join-Path $PSScriptRoot '..\chat-sync\NativeChatSync.psm1')
        )
        Assert-Equal -Actual ($source -match 'Stop-NativeOwnedProcess') -Expected $false
        Assert-True -Condition ($source -match '"--service-config"')
    }

    Invoke-TestCase -Name 'Start binding preserves env-file, service-config, and an absolute UV_ENV_FILE' -Test {
        $captureState = Join-Path $tempRoot 'capture-state'
        $capturePids = Join-Path $tempRoot 'capture-pids'
        $captureLogs = Join-Path $tempRoot 'capture-logs'
        New-Item -ItemType Directory -Path $captureState -Force | Out-Null
        New-Item -ItemType Directory -Path $capturePids -Force | Out-Null
        New-Item -ItemType Directory -Path $captureLogs -Force | Out-Null
        $payload = New-ServiceConfig
        $payload.stateDir = $captureState
        $captureConfigPath = Join-Path $tempRoot 'capture-service.json'
        Write-JsonFile -Path $captureConfigPath -Value $payload
        Write-JsonFile -Path (Join-Path $captureState 'status.json') -Value ([ordered]@{
            pid = 2147483647
            updated_at = [DateTimeOffset]::UtcNow.ToString('o')
            workspace = 'personal'
            intervalSeconds = 30
            exitCode = 0
            threadCount = 0
        })
        $context = [pscustomobject]@{
            RepoRoot = $repoRoot
            ApiRoot = $repoRoot
            PidsRoot = $capturePids
            LogsRoot = $captureLogs
        }
        $global:CapturedChatSyncStartBinding = $null
        function global:Start-NativeOwnedProcess {
            param($Context, $Name, $FilePath, [string[]]$ArgumentList, $WorkingDirectory, $Environment, $ConfigurationTag)
            $global:CapturedChatSyncStartBinding = [pscustomobject]@{
                ArgumentList = [string[]]$ArgumentList
                WorkingDirectory = $WorkingDirectory
                Environment = $Environment
                ConfigurationTag = $ConfigurationTag
            }
            throw 'CAPTURE_ONLY_NO_PROCESS'
        }
        try {
            try {
                Start-NativeChatSyncService `
                    -Context $context `
                    -UvxFile 'unused-uvx.exe' `
                    -UvExactPrefix @('--from', 'uv==0.12.21', 'uv') `
                    -ApiBaseUrl 'http://127.0.0.1:8059' `
                    -ConfigPath $captureConfigPath | Out-Null
            } catch {
                if ($_.Exception.Message -ne 'CAPTURE_ONLY_NO_PROCESS') {
                    throw
                }
            }
        } finally {
            Remove-Item Function:\Start-NativeOwnedProcess -ErrorAction SilentlyContinue
        }

        $captured = $global:CapturedChatSyncStartBinding
        Assert-True -Condition ($null -ne $captured)
        $envFileIndex = [Array]::IndexOf($captured.ArgumentList, '--env-file')
        Assert-True -Condition ($envFileIndex -ge 0)
        Assert-Equal -Actual $captured.ArgumentList[$envFileIndex + 1] -Expected '.env'
        $serviceConfigIndex = [Array]::IndexOf($captured.ArgumentList, '--service-config')
        Assert-True -Condition ($serviceConfigIndex -ge 0)
        Assert-Equal -Actual $captured.ArgumentList[$serviceConfigIndex + 1] -Expected $captureConfigPath
        Assert-Equal -Actual $captured.Environment['UV_ENV_FILE'] -Expected (Join-Path $repoRoot '.env')
        Assert-Equal -Actual @(Get-ChildItem -LiteralPath $capturePids -Force).Count -Expected 0
        Assert-Equal -Actual @(Get-ChildItem -LiteralPath $captureLogs -Force).Count -Expected 0
    }

    Invoke-TestCase -Name 'daemon readiness requires an owned descendant and a fresh successful cycle' -Test {
        Write-JsonFile -Path $configPath -Value (New-ServiceConfig)
        $config = Get-NativeChatSyncServiceConfig -ConfigPath $configPath -RepoRoot $repoRoot
        $tag = Get-NativeChatSyncConfigurationTag -Config $config -ApiBaseUrl 'http://127.0.0.1:8059'
        $owned = [pscustomobject]@{
            Record = [pscustomobject]@{ pid = 100; configurationTag = $tag }
            Process = [pscustomobject]@{ Id = 100 }
        }
        $snapshot = @(
            [pscustomobject]@{ ProcessId = 100; ParentProcessId = 1 },
            [pscustomobject]@{ ProcessId = 150; ParentProcessId = 100 },
            [pscustomobject]@{ ProcessId = 200; ParentProcessId = 150 }
        )
        $now = [DateTimeOffset]::Parse('2026-10-21T12:00:30Z')
        $heartbeat = [ordered]@{
            pid = 200
            updated_at = '2026-10-21T12:00:00+00:00'
            workspace = 'personal'
            intervalSeconds = 30
            exitCode = 0
            threadCount = 4
        }
        Write-JsonFile -Path (Join-Path $stateDir 'status.json') -Value $heartbeat

        $healthy = Get-NativeChatSyncStatus `
            -Context ([pscustomobject]@{ RepoRoot = $repoRoot }) `
            -Config $config `
            -ApiBaseUrl 'http://127.0.0.1:8059' `
            -OwnedProcess $owned `
            -ProcessSnapshot $snapshot `
            -Now $now
        Assert-True -Condition $healthy.Ok
        Assert-Equal -Actual $healthy.ThreadCount -Expected 4

        $heartbeat.exitCode = 1
        Write-JsonFile -Path (Join-Path $stateDir 'status.json') -Value $heartbeat
        $failedCycle = Get-NativeChatSyncStatus `
            -Context ([pscustomobject]@{ RepoRoot = $repoRoot }) `
            -Config $config `
            -ApiBaseUrl 'http://127.0.0.1:8059' `
            -OwnedProcess $owned `
            -ProcessSnapshot $snapshot `
            -Now $now
        Assert-Equal -Actual $failedCycle.Ok -Expected $false
        Assert-Equal -Actual $failedCycle.Reason -Expected 'cycle_failed'
    }

    Invoke-TestCase -Name 'stale one-shot heartbeat and unrelated live PID are not daemon readiness' -Test {
        Write-JsonFile -Path $configPath -Value (New-ServiceConfig)
        $config = Get-NativeChatSyncServiceConfig -ConfigPath $configPath -RepoRoot $repoRoot
        $tag = Get-NativeChatSyncConfigurationTag -Config $config -ApiBaseUrl 'http://127.0.0.1:8059'
        $owned = [pscustomobject]@{
            Record = [pscustomobject]@{ pid = 100; configurationTag = $tag }
            Process = [pscustomobject]@{ Id = 100 }
        }
        $heartbeat = [ordered]@{
            pid = 200
            updated_at = '2026-10-21T12:00:00+00:00'
            workspace = 'personal'
            intervalSeconds = 30
            exitCode = 0
            threadCount = 2
        }
        Write-JsonFile -Path (Join-Path $stateDir 'status.json') -Value $heartbeat
        $deadSnapshot = @(
            [pscustomobject]@{ ProcessId = 100; ParentProcessId = 1 }
        )
        $stale = Get-NativeChatSyncStatus `
            -Context ([pscustomobject]@{ RepoRoot = $repoRoot }) `
            -Config $config `
            -ApiBaseUrl 'http://127.0.0.1:8059' `
            -OwnedProcess $owned `
            -ProcessSnapshot $deadSnapshot `
            -Now ([DateTimeOffset]::Parse('2026-10-21T12:00:30Z'))
        Assert-Equal -Actual $stale.Ok -Expected $false
        Assert-Equal -Actual $stale.State -Expected 'external'

        $external = Get-NativeChatSyncStatus `
            -Context ([pscustomobject]@{ RepoRoot = $repoRoot }) `
            -Config $config `
            -ApiBaseUrl 'http://127.0.0.1:8059' `
            -OwnedProcess $null `
            -ProcessSnapshot @([pscustomobject]@{ ProcessId = 200; ParentProcessId = 1 }) `
            -Now ([DateTimeOffset]::Parse('2026-10-21T12:00:30Z'))
        Assert-Equal -Actual $external.State -Expected 'external'
        Assert-Equal -Actual $external.Reason -Expected 'external_lock'
    }

    Invoke-TestCase -Name 'process ancestry traversal is bounded and rejects cycles' -Test {
        $normal = @(
            [pscustomobject]@{ ProcessId = 100; ParentProcessId = 1 },
            [pscustomobject]@{ ProcessId = 150; ParentProcessId = 100 },
            [pscustomobject]@{ ProcessId = 200; ParentProcessId = 150 }
        )
        Assert-True -Condition (Test-NativeChatSyncProcessDescendant `
            -ProcessId 200 -RootProcessId 100 -ProcessSnapshot $normal -MaxDepth 4)
        $cycle = @(
            [pscustomobject]@{ ProcessId = 200; ParentProcessId = 300 },
            [pscustomobject]@{ ProcessId = 300; ParentProcessId = 200 }
        )
        Assert-Equal -Actual (Test-NativeChatSyncProcessDescendant `
            -ProcessId 200 -RootProcessId 100 -ProcessSnapshot $cycle -MaxDepth 8) -Expected $false
    }
} finally {
    $resolvedTempBase = [System.IO.Path]::GetFullPath([System.IO.Path]::GetTempPath()).TrimEnd('\')
    $resolvedTempRoot = [System.IO.Path]::GetFullPath($tempRoot)
    if (
        (Test-Path -LiteralPath $resolvedTempRoot -PathType Container) -and
        ([System.IO.Path]::GetDirectoryName($resolvedTempRoot).TrimEnd('\') -eq $resolvedTempBase) -and
        ([System.IO.Path]::GetFileName($resolvedTempRoot) -like 'plane-chat-sync-tests-*')
    ) {
        Remove-Item -LiteralPath $resolvedTempRoot -Recurse -Force
    }
}

Write-Host ("Tests: {0} passed, {1} failed" -f $script:Passed, $script:Failed)
if ($script:Failed -gt 0) {
    exit 1
}
