Set-StrictMode -Version 2.0

$script:AllowedConfigNames = @(
    "enabled",
    "workspace",
    "codexExecutable",
    "pollIntervalSeconds",
    "stateDir"
)
$script:HeartbeatNames = @(
    "pid",
    "updated_at",
    "workspace",
    "intervalSeconds",
    "exitCode",
    "threadCount"
)
$script:MaxProcessAncestryDepth = 32

function Get-NativeChatSyncServiceConfigPath {
    param([string] $ConfigPath)

    if ($ConfigPath) {
        return [System.IO.Path]::GetFullPath($ConfigPath)
    }
    if ([string]::IsNullOrWhiteSpace($env:LOCALAPPDATA)) {
        throw "LOCALAPPDATA is required to locate the chat-sync service configuration."
    }
    return [System.IO.Path]::GetFullPath((Join-Path $env:LOCALAPPDATA "plane-chat-sync\service.json"))
}

function Test-NativeChatSyncPathWithinRepository {
    param(
        [Parameter(Mandatory = $true)]
        [string] $Path,
        [Parameter(Mandatory = $true)]
        [string] $RepoRoot
    )

    $candidate = [System.IO.Path]::GetFullPath($Path)
    $root = [System.IO.Path]::GetFullPath($RepoRoot).TrimEnd([char[]]@('\', '/'))
    if ([string]::Equals($candidate, $root, [StringComparison]::OrdinalIgnoreCase)) {
        return $true
    }
    return $candidate.StartsWith(
        ($root + [System.IO.Path]::DirectorySeparatorChar),
        [StringComparison]::OrdinalIgnoreCase
    )
}

function Get-NativeChatSyncServiceConfig {
    [CmdletBinding()]
    param(
        [string] $ConfigPath,
        [string] $RepoRoot = (Get-Location).Path
    )

    $resolvedPath = Get-NativeChatSyncServiceConfigPath -ConfigPath $ConfigPath
    if (Test-NativeChatSyncPathWithinRepository -Path $resolvedPath -RepoRoot $RepoRoot) {
        throw "The chat-sync service configuration must live outside the Plane repository."
    }
    if (-not (Test-Path -LiteralPath $resolvedPath -PathType Leaf)) {
        return [pscustomobject]@{
            ConfigPath = $resolvedPath
            Exists = $false
            Enabled = $false
            ConfigHash = $null
        }
    }

    try {
        $rawText = [System.IO.File]::ReadAllText($resolvedPath)
        $raw = $rawText | ConvertFrom-Json
    } catch {
        throw "The chat-sync service configuration is not valid JSON."
    }
    if ($null -eq $raw -or $raw -is [System.Array]) {
        throw "The chat-sync service configuration must be one JSON object."
    }

    $propertyNames = @($raw.PSObject.Properties.Name)
    $unsupported = @($propertyNames | Where-Object { $_ -notin $script:AllowedConfigNames })
    $secretLike = @($propertyNames | Where-Object {
        $_ -match "(?i)(api[_-]?key|secret|password|passwd|token)"
    })
    if ($unsupported.Count -gt 0 -or $secretLike.Count -gt 0) {
        throw "The chat-sync service configuration contains unsupported or secret-like fields."
    }

    $enabledProperty = $raw.PSObject.Properties["enabled"]
    if ($null -eq $enabledProperty -or $enabledProperty.Value -isnot [bool]) {
        throw "The chat-sync service configuration requires a boolean enabled field."
    }

    $sha256 = [System.Security.Cryptography.SHA256]::Create()
    try {
        $configHash = ([System.BitConverter]::ToString(
            $sha256.ComputeHash([System.Text.Encoding]::UTF8.GetBytes($rawText))
        )).Replace("-", "").ToLowerInvariant()
    } finally {
        $sha256.Dispose()
    }

    if (-not $enabledProperty.Value) {
        return [pscustomobject]@{
            ConfigPath = $resolvedPath
            Exists = $true
            Enabled = $false
            ConfigHash = $configHash
        }
    }

    foreach ($name in $script:AllowedConfigNames) {
        if ($null -eq $raw.PSObject.Properties[$name]) {
            throw "The enabled chat-sync service configuration is missing required fields."
        }
    }

    $workspace = [string] $raw.workspace
    if ($workspace -notmatch "^[A-Za-z0-9][A-Za-z0-9_-]{0,254}$") {
        throw "The chat-sync workspace must be a valid workspace slug."
    }

    $codexExecutable = [string] $raw.codexExecutable
    if ([string]::IsNullOrWhiteSpace($codexExecutable) -or $codexExecutable.Length -gt 2048) {
        throw "The chat-sync codexExecutable must be a non-empty path or command name."
    }

    $intervalValue = $raw.pollIntervalSeconds
    if (
        $intervalValue -is [bool] -or
        (
            $intervalValue -isnot [int] -and
            $intervalValue -isnot [long] -and
            $intervalValue -isnot [decimal]
        )
    ) {
        throw "The chat-sync pollIntervalSeconds must be an integer."
    }
    $interval = [int] $intervalValue
    if ([double] $intervalValue -ne $interval -or $interval -lt 5 -or $interval -gt 3600) {
        throw "The chat-sync pollIntervalSeconds must be between 5 and 3600."
    }

    $stateDirectoryValue = [string] $raw.stateDir
    if (
        [string]::IsNullOrWhiteSpace($stateDirectoryValue) -or
        -not [System.IO.Path]::IsPathRooted($stateDirectoryValue)
    ) {
        throw "The chat-sync stateDir must be an absolute path."
    }
    $stateDirectory = [System.IO.Path]::GetFullPath($stateDirectoryValue)
    if (Test-NativeChatSyncPathWithinRepository -Path $stateDirectory -RepoRoot $RepoRoot) {
        throw "The chat-sync stateDir must live outside the Plane repository."
    }

    return [pscustomobject]@{
        ConfigPath = $resolvedPath
        Exists = $true
        Enabled = $true
        ConfigHash = $configHash
        Workspace = $workspace
        CodexExecutable = $codexExecutable
        PollIntervalSeconds = $interval
        StateDir = $stateDirectory
    }
}

function Get-NativeChatSyncConfigurationTag {
    param(
        [Parameter(Mandatory = $true)]
        [pscustomobject] $Config,
        [Parameter(Mandatory = $true)]
        [string] $ApiBaseUrl
    )

    if (-not $Config.Enabled -or [string]::IsNullOrWhiteSpace($Config.ConfigHash)) {
        throw "An enabled chat-sync configuration is required for its process tag."
    }
    return ("native-chat-sync:{0}:api:{1}" -f $Config.ConfigHash, $ApiBaseUrl)
}

function Test-NativeChatSyncProcessDescendant {
    param(
        [Parameter(Mandatory = $true)]
        [int] $ProcessId,
        [Parameter(Mandatory = $true)]
        [int] $RootProcessId,
        [Parameter(Mandatory = $true)]
        [object[]] $ProcessSnapshot,
        [int] $MaxDepth = $script:MaxProcessAncestryDepth
    )

    if ($ProcessId -le 0 -or $RootProcessId -le 0 -or $ProcessId -eq $RootProcessId) {
        return $false
    }

    $parents = @{}
    foreach ($item in @($ProcessSnapshot)) {
        if ($null -eq $item) {
            continue
        }
        $parents[[int] $item.ProcessId] = [int] $item.ParentProcessId
    }

    $seen = @{}
    $current = $ProcessId
    for ($depth = 0; $depth -lt [Math]::Min($MaxDepth, $script:MaxProcessAncestryDepth); $depth++) {
        if ($seen.ContainsKey($current) -or -not $parents.ContainsKey($current)) {
            return $false
        }
        $seen[$current] = $true
        $current = [int] $parents[$current]
        if ($current -eq $RootProcessId) {
            return $true
        }
        if ($current -le 0) {
            return $false
        }
    }
    return $false
}

function Get-NativeChatSyncStatus {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [pscustomobject] $Context,
        [Parameter(Mandatory = $true)]
        [pscustomobject] $Config,
        [Parameter(Mandatory = $true)]
        [string] $ApiBaseUrl,
        [AllowNull()]
        [pscustomobject] $OwnedProcess,
        [object[]] $ProcessSnapshot,
        [DateTimeOffset] $Now = [DateTimeOffset]::UtcNow
    )

    if (-not $Config.Enabled) {
        return [pscustomobject]@{
            Ok = $true
            State = "disabled"
            Reason = "disabled"
            OwnedProcess = $null
            Heartbeat = $null
        }
    }

    if (-not $PSBoundParameters.ContainsKey("OwnedProcess")) {
        $OwnedProcess = Get-NativeOwnedProcess -Context $Context -Name "chat-sync"
    }
    if (-not $PSBoundParameters.ContainsKey("ProcessSnapshot")) {
        $ProcessSnapshot = @(Get-CimInstance Win32_Process -ErrorAction Stop |
            Select-Object ProcessId, ParentProcessId)
    } else {
        $ProcessSnapshot = @($ProcessSnapshot)
    }

    $statusPath = Join-Path $Config.StateDir "status.json"
    if (-not (Test-Path -LiteralPath $statusPath -PathType Leaf)) {
        return [pscustomobject]@{
            Ok = $false
            State = "missing"
            Reason = "status_missing"
            StatusPath = $statusPath
            OwnedProcess = $OwnedProcess
            Heartbeat = $null
        }
    }

    try {
        $heartbeat = Get-Content -Raw -LiteralPath $statusPath | ConvertFrom-Json
    } catch {
        return [pscustomobject]@{
            Ok = $false
            State = "invalid"
            Reason = "status_invalid"
            StatusPath = $statusPath
            OwnedProcess = $OwnedProcess
            Heartbeat = $null
        }
    }
    if ($null -eq $heartbeat -or $heartbeat -is [System.Array]) {
        return [pscustomobject]@{
            Ok = $false
            State = "invalid"
            Reason = "status_invalid"
            StatusPath = $statusPath
            OwnedProcess = $OwnedProcess
            Heartbeat = $null
        }
    }

    $heartbeatNames = @($heartbeat.PSObject.Properties.Name)
    if (
        $heartbeatNames.Count -ne $script:HeartbeatNames.Count -or
        @($script:HeartbeatNames | Where-Object { $_ -notin $heartbeatNames }).Count -gt 0
    ) {
        return [pscustomobject]@{
            Ok = $false
            State = "invalid"
            Reason = "status_fields"
            StatusPath = $statusPath
            OwnedProcess = $OwnedProcess
            Heartbeat = $heartbeat
        }
    }

    try {
        $heartbeatPid = [int] $heartbeat.pid
        $intervalSeconds = [int] $heartbeat.intervalSeconds
        $exitCode = [int] $heartbeat.exitCode
        $threadCount = [int] $heartbeat.threadCount
        $updatedAt = [DateTimeOffset]::Parse(
            [string] $heartbeat.updated_at,
            [System.Globalization.CultureInfo]::InvariantCulture,
            [System.Globalization.DateTimeStyles]::RoundtripKind
        )
    } catch {
        return [pscustomobject]@{
            Ok = $false
            State = "invalid"
            Reason = "status_values"
            StatusPath = $statusPath
            OwnedProcess = $OwnedProcess
            Heartbeat = $heartbeat
        }
    }

    $liveCommandPid = @($ProcessSnapshot | Where-Object {
        $null -ne $_ -and [int] $_.ProcessId -eq $heartbeatPid
    }).Count -gt 0
    if ($null -eq $OwnedProcess) {
        return [pscustomobject]@{
            Ok = $false
            State = if ($liveCommandPid) { "external" } else { "missing-owner" }
            Reason = if ($liveCommandPid) { "external_lock" } else { "owned_process_missing" }
            StatusPath = $statusPath
            OwnedProcess = $null
            Heartbeat = $heartbeat
            CommandPid = $heartbeatPid
        }
    }

    $ownedRootPid = [int] $OwnedProcess.Process.Id
    $expectedTag = Get-NativeChatSyncConfigurationTag -Config $Config -ApiBaseUrl $ApiBaseUrl
    $tagProperty = $OwnedProcess.Record.PSObject.Properties["configurationTag"]
    if (
        $null -eq $tagProperty -or
        -not [string]::Equals([string] $tagProperty.Value, $expectedTag, [StringComparison]::Ordinal)
    ) {
        return [pscustomobject]@{
            Ok = $false
            State = "config-mismatch"
            Reason = "configuration_changed"
            StatusPath = $statusPath
            OwnedProcess = $OwnedProcess
            Heartbeat = $heartbeat
            CommandPid = $heartbeatPid
        }
    }

    if (-not $liveCommandPid -or -not (Test-NativeChatSyncProcessDescendant `
        -ProcessId $heartbeatPid `
        -RootProcessId $ownedRootPid `
        -ProcessSnapshot $ProcessSnapshot)) {
        return [pscustomobject]@{
            Ok = $false
            State = "external"
            Reason = "heartbeat_not_owned"
            StatusPath = $statusPath
            OwnedProcess = $OwnedProcess
            Heartbeat = $heartbeat
            CommandPid = $heartbeatPid
        }
    }

    $ageSeconds = ($Now.ToUniversalTime() - $updatedAt.ToUniversalTime()).TotalSeconds
    $freshnessSeconds = [Math]::Max(120, (3 * $Config.PollIntervalSeconds))
    if ($ageSeconds -lt -60 -or $ageSeconds -gt $freshnessSeconds) {
        return [pscustomobject]@{
            Ok = $false
            State = "stale"
            Reason = "heartbeat_stale"
            StatusPath = $statusPath
            OwnedProcess = $OwnedProcess
            Heartbeat = $heartbeat
            CommandPid = $heartbeatPid
            AgeSeconds = $ageSeconds
        }
    }
    if (-not [string]::Equals([string] $heartbeat.workspace, $Config.Workspace, [StringComparison]::Ordinal)) {
        return [pscustomobject]@{
            Ok = $false
            State = "mismatch"
            Reason = "workspace_mismatch"
            StatusPath = $statusPath
            OwnedProcess = $OwnedProcess
            Heartbeat = $heartbeat
            CommandPid = $heartbeatPid
            AgeSeconds = $ageSeconds
        }
    }
    if ($intervalSeconds -ne $Config.PollIntervalSeconds) {
        return [pscustomobject]@{
            Ok = $false
            State = "mismatch"
            Reason = "interval_mismatch"
            StatusPath = $statusPath
            OwnedProcess = $OwnedProcess
            Heartbeat = $heartbeat
            CommandPid = $heartbeatPid
            AgeSeconds = $ageSeconds
        }
    }
    if ($exitCode -ne 0 -or $threadCount -lt 0) {
        return [pscustomobject]@{
            Ok = $false
            State = "unhealthy"
            Reason = "cycle_failed"
            StatusPath = $statusPath
            OwnedProcess = $OwnedProcess
            Heartbeat = $heartbeat
            CommandPid = $heartbeatPid
            AgeSeconds = $ageSeconds
        }
    }

    return [pscustomobject]@{
        Ok = $true
        State = "running"
        Reason = "fresh_success"
        StatusPath = $statusPath
        OwnedProcess = $OwnedProcess
        Heartbeat = $heartbeat
        CommandPid = $heartbeatPid
        AgeSeconds = $ageSeconds
        ThreadCount = $threadCount
    }
}

function Start-NativeChatSyncService {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [pscustomobject] $Context,
        [Parameter(Mandatory = $true)]
        [string] $UvxFile,
        [Parameter(Mandatory = $true)]
        [string[]] $UvExactPrefix,
        [Parameter(Mandatory = $true)]
        [string] $ApiBaseUrl,
        [int] $StartupTimeoutSeconds = 120,
        [string] $ConfigPath
    )

    $config = Get-NativeChatSyncServiceConfig `
        -ConfigPath $ConfigPath `
        -RepoRoot $Context.RepoRoot
    if (-not $config.Exists -or -not $config.Enabled) {
        return [pscustomobject]@{
            State = "disabled"
            Config = $config
            OwnedProcess = $null
            Heartbeat = $null
        }
    }

    $nativeApiBaseUrl = Get-NativeApiBaseUrl
    if (-not [string]::Equals($ApiBaseUrl, $nativeApiBaseUrl, [StringComparison]::Ordinal)) {
        throw "The chat-sync Plane URL must be the native loopback API endpoint."
    }

    $configurationTag = Get-NativeChatSyncConfigurationTag -Config $config -ApiBaseUrl $ApiBaseUrl
    $owned = Get-NativeOwnedProcess -Context $Context -Name "chat-sync"
    $status = Get-NativeChatSyncStatus `
        -Context $Context `
        -Config $config `
        -ApiBaseUrl $ApiBaseUrl `
        -OwnedProcess $owned

    if ($owned) {
        if ($status.Ok) {
            return [pscustomobject]@{
                State = "reused"
                Config = $config
                OwnedProcess = $owned
                Heartbeat = $status.Heartbeat
            }
        }
        throw (
            "chat-sync PID {0} is live but not reusable ({1}); no process was terminated. " +
            "Disable or change the service configuration and wait for the daemon to exit before retrying."
        ) -f $owned.Record.pid, $status.Reason
    }
    if ($status.State -eq "external") {
        throw "An external chat-sync worker holds the state lock; no process was terminated."
    }

    $arguments = $UvExactPrefix + @(
        "run", "--env-file", ".env",
        "manage.py", "sync_codex_chats",
        "--workspace", $config.Workspace,
        "--codex-executable", $config.CodexExecutable,
        "--plane-base-url", $ApiBaseUrl,
        "--state-dir", $config.StateDir,
        "--interval", [string] $config.PollIntervalSeconds,
        "--service-config", $config.ConfigPath,
        "--settings=plane.settings.local"
    )
    # Start-Process can preserve a caller's relative UV_ENV_FILE under PowerShell 7.
    # Pin the same native .env by absolute path so uv always has a usable value.
    $environment = @{
        UV_ENV_FILE = [System.IO.Path]::GetFullPath((Join-Path $Context.ApiRoot ".env"))
    }
    $record = Start-NativeOwnedProcess `
        -Context $Context `
        -Name "chat-sync" `
        -FilePath $UvxFile `
        -ArgumentList $arguments `
        -WorkingDirectory $Context.ApiRoot `
        -Environment $environment `
        -ConfigurationTag $configurationTag

    $deadline = [DateTime]::UtcNow.AddSeconds($StartupTimeoutSeconds)
    do {
        Start-Sleep -Milliseconds 1000
        $owned = Get-NativeOwnedProcess -Context $Context -Name "chat-sync"
        if (-not $owned) {
            throw ("chat-sync exited during startup. Inspect {0} and {1}." -f $record.stdout, $record.stderr)
        }
        $status = Get-NativeChatSyncStatus `
            -Context $Context `
            -Config $config `
            -ApiBaseUrl $ApiBaseUrl `
            -OwnedProcess $owned
        if ($status.Ok) {
            return [pscustomobject]@{
                State = "started"
                Config = $config
                OwnedProcess = $owned
                Heartbeat = $status.Heartbeat
            }
        }
    } while ([DateTime]::UtcNow -lt $deadline)

    throw (
        "chat-sync did not publish a fresh successful heartbeat within {0} seconds; " +
        "the live process was left running and was not terminated."
    ) -f $StartupTimeoutSeconds
}

Export-ModuleMember -Function @(
    "Get-NativeChatSyncServiceConfig",
    "Get-NativeChatSyncServiceConfigPath",
    "Get-NativeChatSyncConfigurationTag",
    "Get-NativeChatSyncStatus",
    "Test-NativeChatSyncProcessDescendant",
    "Start-NativeChatSyncService"
)
