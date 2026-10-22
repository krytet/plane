Set-StrictMode -Version 2.0

$script:NativeApiPort = 8059
$script:NativeWebPort = 3059
$script:ServiceNamePattern = '^[a-z][a-z0-9-]{1,31}$'
$script:AllowedEnvironmentNames = @(
    'ALLUSERSPROFILE', 'APPDATA', 'COMSPEC', 'COMPUTERNAME', 'ComSpec',
    'COREPACK_ENABLE_DOWNLOAD_PROMPT', 'COREPACK_HOME', 'DriverData',
    'HOMEDRIVE', 'HOMEPATH', 'HOME', 'LANG', 'LC_ALL', 'LOCALAPPDATA',
    'NODE_EXTRA_CA_CERTS', 'NUMBER_OF_PROCESSORS', 'OS', 'PATH', 'PATHEXT',
    'PNPM_HOME', 'PROCESSOR_ARCHITECTURE', 'ProgramData', 'ProgramFiles',
    'ProgramFiles(x86)', 'PSModulePath', 'PUBLIC', 'SystemDrive', 'SystemRoot',
    'TEMP', 'TMP', 'TZ', 'USERDOMAIN', 'USERNAME', 'USERPROFILE', 'WINDIR',
    'UV_CACHE_DIR', 'UV_HTTP_TIMEOUT', 'UV_NATIVE_TLS', 'UV_PYTHON_INSTALL_DIR',
    'UV_TOOL_DIR'
)

function Get-NativeRepoRoot {
    param(
        [Parameter(Mandatory = $true)]
        [string] $StartPath
    )

    $current = [System.IO.Path]::GetFullPath($StartPath)
    if (-not (Test-Path -LiteralPath $current -PathType Container)) {
        $current = Split-Path -Parent $current
    }

    while ($current) {
        if (
            (Test-Path -LiteralPath (Join-Path $current 'pnpm-workspace.yaml') -PathType Leaf) -and
            (Test-Path -LiteralPath (Join-Path $current 'apps\api\pyproject.toml') -PathType Leaf)
        ) {
            return (Resolve-Path -LiteralPath $current).Path
        }

        $parent = Split-Path -Parent $current
        if ($parent -eq $current) {
            break
        }
        $current = $parent
    }

    throw 'Could not locate the Plane repository root.'
}

function New-NativeContext {
    param(
        [Parameter(Mandatory = $true)]
        [string] $RepoRoot
    )

    $root = (Resolve-Path -LiteralPath $RepoRoot).Path
    $localRoot = Join-Path $root '.local-native'

    return [pscustomobject]@{
        RepoRoot = $root
        ApiRoot = Join-Path $root 'apps\api'
        LocalRoot = $localRoot
        ToolsRoot = Join-Path $localRoot 'tools'
        DataRoot = Join-Path $localRoot 'data'
        LogsRoot = Join-Path $localRoot 'logs'
        PidsRoot = Join-Path $localRoot 'pids'
        TempRoot = Join-Path $localRoot 'temp'
    }
}

function Get-NativeApiPort {
    return $script:NativeApiPort
}

function Get-NativeApiBaseUrl {
    return ('http://127.0.0.1:{0}' -f $script:NativeApiPort)
}

function Get-NativeWebPort {
    return $script:NativeWebPort
}

function Get-NativeWebBaseUrl {
    return ('http://localhost:{0}' -f $script:NativeWebPort)
}

function Test-NativeApiEndpointTarget {
    param(
        [AllowNull()]
        [string] $Actual,
        [Parameter(Mandatory = $true)]
        [string] $Expected
    )

    return [string]::Equals([string] $Actual, $Expected, [StringComparison]::Ordinal)
}

function Test-NativeStorageCorsOrigin {
    param(
        [Parameter(Mandatory = $true)]
        [uri] $Endpoint,
        [Parameter(Mandatory = $true)]
        [string] $Bucket,
        [Parameter(Mandatory = $true)]
        [string] $Origin,
        [scriptblock] $RequestProvider
    )

    if (-not $RequestProvider) {
        $RequestProvider = {
            param([string] $Uri, [hashtable] $Headers)
            Invoke-WebRequest `
                -UseBasicParsing `
                -Uri $Uri `
                -Method Options `
                -Headers $Headers `
                -TimeoutSec 5 `
                -ErrorAction Stop
        }
    }

    $uri = $Endpoint.AbsoluteUri.TrimEnd('/') + '/' + $Bucket.Trim('/') + '/plane-cors-preflight'
    $headers = @{
        Origin = $Origin
        'Access-Control-Request-Method' = 'GET'
    }
    try {
        $response = & $RequestProvider $uri $headers
        $allowOrigin = [string] ($response.Headers['Access-Control-Allow-Origin'])
        return [string]::Equals($allowOrigin, $Origin, [StringComparison]::Ordinal)
    } catch {
        return $false
    }
}

function Test-NativeOwnedProcessConfiguration {
    param(
        [AllowNull()]
        [pscustomobject] $OwnedProcess,
        [string] $ConfigurationTag = ''
    )

    if (-not $ConfigurationTag) {
        return $true
    }
    if ($null -eq $OwnedProcess) {
        return $false
    }

    $tagProperty = $OwnedProcess.Record.PSObject.Properties['configurationTag']
    return [bool] ($tagProperty -and [string] $tagProperty.Value -eq $ConfigurationTag)
}

function ConvertFrom-NativeProcessStartTime {
    param(
        [Parameter(Mandatory = $true)]
        $Value
    )

    if ($Value -is [DateTime]) {
        return $Value.ToUniversalTime()
    }
    if ($Value -is [DateTimeOffset]) {
        return $Value.UtcDateTime
    }

    $parsed = [DateTimeOffset]::MinValue
    if (-not [DateTimeOffset]::TryParse(
        [string] $Value,
        [System.Globalization.CultureInfo]::InvariantCulture,
        [System.Globalization.DateTimeStyles]::RoundtripKind,
        [ref] $parsed
    )) {
        throw "The owned process start time is invalid."
    }
    return $parsed.UtcDateTime
}

function Initialize-NativeContext {
    param(
        [Parameter(Mandatory = $true)]
        [pscustomobject] $Context
    )

    foreach ($path in @(
        $Context.LocalRoot,
        $Context.ToolsRoot,
        $Context.DataRoot,
        $Context.LogsRoot,
        $Context.PidsRoot,
        $Context.TempRoot
    )) {
        if (-not (Test-Path -LiteralPath $path -PathType Container)) {
            New-Item -ItemType Directory -Path $path -Force | Out-Null
        }
    }
}

function Write-NativeStatus {
    param(
        [ValidateSet('INFO', 'WARN', 'ERROR', 'OK')]
        [string] $Level = 'INFO',
        [Parameter(Mandatory = $true)]
        [string] $Message
    )

    $prefix = switch ($Level) {
        'OK' { '[OK]   ' }
        'WARN' { '[WARN] ' }
        'ERROR' { '[ERROR]' }
        default { '[INFO] ' }
    }
    Write-Host ("{0} {1}" -f $prefix, $Message)
}

function Assert-NativeCommand {
    param(
        [Parameter(Mandatory = $true)]
        [string] $Name,
        [string] $FriendlyName = $Name
    )

    $command = Get-Command $Name -ErrorAction SilentlyContinue
    if (-not $command) {
        throw ("{0} is required but was not found on PATH." -f $FriendlyName)
    }
    return $command.Source
}

function ConvertTo-NativeProcessArgument {
    param(
        [AllowEmptyString()]
        [Parameter(Mandatory = $true)]
        [string] $Value
    )

    if ($Value.Length -gt 0 -and $Value -notmatch '[\s"]') {
        return $Value
    }

    $builder = New-Object System.Text.StringBuilder
    [void] $builder.Append('"')
    $backslashes = 0

    foreach ($character in $Value.ToCharArray()) {
        if ($character -eq '\') {
            $backslashes++
            continue
        }

        if ($character -eq '"') {
            [void] $builder.Append(('\' * (($backslashes * 2) + 1)))
            [void] $builder.Append('"')
            $backslashes = 0
            continue
        }

        if ($backslashes -gt 0) {
            [void] $builder.Append(('\' * $backslashes))
            $backslashes = 0
        }
        [void] $builder.Append($character)
    }

    if ($backslashes -gt 0) {
        [void] $builder.Append(('\' * ($backslashes * 2)))
    }
    [void] $builder.Append('"')
    return $builder.ToString()
}

function Invoke-NativeCapturedProcess {
    param(
        [Parameter(Mandatory = $true)]
        [string] $FilePath,
        [string[]] $ArgumentList = @(),
        [Parameter(Mandatory = $true)]
        [string] $WorkingDirectory,
        [int] $TimeoutSeconds = 120,
        [hashtable] $Environment = @{},
        [switch] $ClearEnvFileVariable
    )

    $startInfo = New-Object System.Diagnostics.ProcessStartInfo
    $startInfo.FileName = $FilePath
    $startInfo.WorkingDirectory = $WorkingDirectory
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    $startInfo.EnvironmentVariables.Clear()
    foreach ($name in $script:AllowedEnvironmentNames) {
        $currentValue = [Environment]::GetEnvironmentVariable($name, 'Process')
        if ($null -ne $currentValue) {
            $startInfo.EnvironmentVariables[$name] = $currentValue
        }
    }

    $quotedArguments = @($ArgumentList | ForEach-Object { ConvertTo-NativeProcessArgument $_ })
    $startInfo.Arguments = ($quotedArguments -join ' ')

    foreach ($key in $Environment.Keys) {
        $startInfo.EnvironmentVariables[$key] = [string] $Environment[$key]
    }

    $process = New-Object System.Diagnostics.Process
    $process.StartInfo = $startInfo
    if (-not $process.Start()) {
        throw ("Failed to start {0}." -f $FilePath)
    }

    $stdoutTask = $process.StandardOutput.ReadToEndAsync()
    $stderrTask = $process.StandardError.ReadToEndAsync()
    $timeoutMilliseconds = [Math]::Max(1000, $TimeoutSeconds * 1000)

    if (-not $process.WaitForExit($timeoutMilliseconds)) {
        try {
            $process.Kill()
            $process.WaitForExit(5000)
        } catch {
            # The command may have exited between the timeout and Kill().
        }
        throw ("Command timed out after {0} seconds: {1}" -f $TimeoutSeconds, $FilePath)
    }

    $stdout = $stdoutTask.Result
    $stderr = $stderrTask.Result
    return [pscustomobject]@{
        ExitCode = $process.ExitCode
        StandardOutput = $stdout
        StandardError = $stderr
    }
}

function Get-NativeToolCommands {
    param(
        [Parameter(Mandatory = $true)]
        [pscustomobject] $Context
    )

    $corepack = Get-Command corepack.cmd -ErrorAction SilentlyContinue
    if ($corepack) {
        $pnpmFile = $corepack.Source
        $pnpmPrefix = @('pnpm')
    } else {
        $pnpm = Get-Command pnpm.cmd -ErrorAction SilentlyContinue
        if (-not $pnpm) {
            throw 'Neither corepack.cmd nor pnpm.cmd was found. Install Node.js 22.22 or newer.'
        }
        $pnpmFile = $pnpm.Source
        $pnpmPrefix = @('dlx', 'pnpm@11.10.0')
    }

    $uvxFile = Assert-NativeCommand -Name 'uvx.exe' -FriendlyName 'uvx (uv)'
    return [pscustomobject]@{
        PnpmFile = $pnpmFile
        PnpmPrefix = $pnpmPrefix
        UvxFile = $uvxFile
        UvVersion = '0.12.21'
        PnpmVersion = '11.10.0'
        UvExactPrefix = @('--from', 'uv==0.12.21', 'uv')
    }
}

function Get-NativeSiloRelease {
    param(
        [string] $Tag = 'RELEASE.2026-09-16T00-00-00Z'
    )

    $encodedTag = [Uri]::EscapeDataString($Tag)
    $releaseUri = "https://api.github.com/repos/pgsty/silo/releases/tags/$encodedTag"
    $headers = @{
        Accept = 'application/vnd.github+json'
        'User-Agent' = 'plane-local-native-setup'
    }
    $release = Invoke-RestMethod -Uri $releaseUri -Headers $headers -UseBasicParsing

    $windowsAssets = @($release.assets | Where-Object {
        $_.name -match '^silo_.*_windows_amd64\.tar\.gz$'
    })
    $checksumAssets = @($release.assets | Where-Object {
        $_.name -match '^silo_.*_checksums\.txt$'
    })

    if ($release.tag_name -ne $Tag -or $windowsAssets.Count -ne 1 -or $checksumAssets.Count -lt 1) {
        throw ("Pinned Silo release {0} does not expose one Windows amd64 archive and checksum manifest." -f $Tag)
    }

    return [pscustomobject]@{
        Tag = $release.tag_name
        ArchiveName = $windowsAssets[0].name
        ArchiveUri = [Uri] $windowsAssets[0].browser_download_url
        ChecksumAssets = $checksumAssets
    }
}

function ConvertFrom-NativeChecksumManifest {
    param(
        [AllowEmptyString()]
        [Parameter(Mandatory = $true)]
        [string] $Manifest,
        [Parameter(Mandatory = $true)]
        [string] $ArchiveName
    )

    $checksumValues = @()
    foreach ($line in ($Manifest -split "`r?`n")) {
        if ($line -match '^([a-fA-F0-9]{64})\s+\*?(.+?)\s*$') {
            if ([System.IO.Path]::GetFileName($Matches[2]) -eq $ArchiveName) {
                $checksumValues += $Matches[1].ToLowerInvariant()
            }
        } elseif ($line -match '^SHA256\s+\((.+?)\)\s*=\s*([a-fA-F0-9]{64})\s*$') {
            if ([System.IO.Path]::GetFileName($Matches[1]) -eq $ArchiveName) {
                $checksumValues += $Matches[2].ToLowerInvariant()
            }
        }
    }

    $unique = @($checksumValues | Select-Object -Unique)
    if ($unique.Count -ne 1) {
        throw ("No unambiguous SHA256 published for {0}." -f $ArchiveName)
    }
    return $unique[0]
}

function Resolve-NativeSiloChecksum {
    param(
        [Parameter(Mandatory = $true)]
        [pscustomobject] $Release
    )

    $checksumValues = @()
    foreach ($asset in $Release.ChecksumAssets) {
        $headers = @{ 'User-Agent' = 'plane-local-native-setup' }
        try {
            $manifestContent = (Invoke-WebRequest -UseBasicParsing -Uri $asset.browser_download_url -Headers $headers).Content
            if ($manifestContent -is [byte[]]) {
                $manifest = [Text.Encoding]::UTF8.GetString($manifestContent)
            } else {
                $manifest = [string] $manifestContent
            }
        } catch {
            continue
        }

        try {
            $checksumValues += (ConvertFrom-NativeChecksumManifest `
                -Manifest $manifest `
                -ArchiveName $Release.ArchiveName)
        } catch {
            # The release may publish several manifests; only one needs the archive.
        }
    }

    $unique = @($checksumValues | Select-Object -Unique)
    if ($unique.Count -ne 1) {
        throw ("No unambiguous SHA256 published in the Silo checksum manifests for {0}." -f $Release.ArchiveName)
    }
    return $unique[0]
}

function Test-NativeTcpEndpoint {
    param(
        [Parameter(Mandatory = $true)]
        [string] $HostName,
        [Parameter(Mandatory = $true)]
        [int] $Port,
        [int] $TimeoutMilliseconds = 1500
    )

    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $async = $client.BeginConnect($HostName, $Port, $null, $null)
        if (-not $async.AsyncWaitHandle.WaitOne($TimeoutMilliseconds)) {
            return $false
        }
        $client.EndConnect($async)
        return $client.Connected
    } catch {
        return $false
    } finally {
        $client.Close()
    }
}

function Wait-NativeTcpEndpoint {
    param(
        [Parameter(Mandatory = $true)]
        [string] $HostName,
        [Parameter(Mandatory = $true)]
        [int] $Port,
        [int] $TimeoutSeconds = 30,
        [int] $PollMilliseconds = 500
    )

    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    do {
        if (Test-NativeTcpEndpoint -HostName $HostName -Port $Port) {
            return $true
        }
        Start-Sleep -Milliseconds $PollMilliseconds
    } while ([DateTime]::UtcNow -lt $deadline)
    return $false
}

function Test-NativeHttpEndpoint {
    param(
        [Parameter(Mandatory = $true)]
        [uri] $Uri,
        [int[]] $ExpectedStatusCodes = @(200),
        [int] $TimeoutSeconds = 5
    )

    try {
        $response = Invoke-WebRequest -UseBasicParsing -Uri $Uri -Method Get -TimeoutSec $TimeoutSeconds
        return ($ExpectedStatusCodes -contains [int] $response.StatusCode)
    } catch {
        if ($_.Exception.Response -and $_.Exception.Response.StatusCode) {
            return ($ExpectedStatusCodes -contains [int] $_.Exception.Response.StatusCode)
        }
        return $false
    }
}

function Start-NativeOwnedProcess {
    param(
        [Parameter(Mandatory = $true)]
        [pscustomobject] $Context,
        [Parameter(Mandatory = $true)]
        [ValidatePattern('^[a-z][a-z0-9-]{1,31}$')]
        [string] $Name,
        [Parameter(Mandatory = $true)]
        [string] $FilePath,
        [string[]] $ArgumentList = @(),
        [Parameter(Mandatory = $true)]
        [string] $WorkingDirectory,
        [hashtable] $Environment = @{},
        [string] $ConfigurationTag = ''
    )

    Initialize-NativeContext -Context $Context
    $logName = $Name
    $stdoutPath = Join-Path $Context.LogsRoot ($logName + '.out.log')
    $stderrPath = Join-Path $Context.LogsRoot ($logName + '.err.log')
    $quotedArguments = @($ArgumentList | ForEach-Object { ConvertTo-NativeProcessArgument $_ })
    $currentEnvironment = @{}
    foreach ($item in @([Environment]::GetEnvironmentVariables('Process').GetEnumerator())) {
        $currentEnvironment[[string] $item.Key] = [string] $item.Value
    }
    foreach ($key in $Environment.Keys) {
        [Environment]::SetEnvironmentVariable($key, [string] $Environment[$key], 'Process')
    }
    $allowed = @{}
    foreach ($environmentName in $script:AllowedEnvironmentNames) {
        $allowed[$environmentName] = $true
    }
    foreach ($key in $currentEnvironment.Keys) {
        if (-not $allowed.ContainsKey($key) -and -not $Environment.ContainsKey($key)) {
            [Environment]::SetEnvironmentVariable($key, $null, 'Process')
        }
    }
    try {
        $process = Start-Process `
            -FilePath $FilePath `
            -ArgumentList $quotedArguments `
            -WorkingDirectory $WorkingDirectory `
            -WindowStyle Hidden `
            -RedirectStandardOutput $stdoutPath `
            -RedirectStandardError $stderrPath `
            -PassThru
    } finally {
        foreach ($key in $Environment.Keys) {
            if (-not $currentEnvironment.ContainsKey($key)) {
                [Environment]::SetEnvironmentVariable($key, $null, 'Process')
            }
        }
        foreach ($key in $currentEnvironment.Keys) {
            [Environment]::SetEnvironmentVariable($key, $currentEnvironment[$key], 'Process')
        }
    }

    $record = [ordered]@{
        name = $Name
        pid = $process.Id
        startedAtUtc = $process.StartTime.ToUniversalTime().ToString('o')
        executable = [System.IO.Path]::GetFullPath($FilePath)
        workingDirectory = [System.IO.Path]::GetFullPath($WorkingDirectory)
        stdout = $stdoutPath
        stderr = $stderrPath
        configurationTag = $ConfigurationTag
    }
    $pidPath = Join-Path $Context.PidsRoot ($Name + '.json')
    [System.IO.File]::WriteAllText(
        $pidPath,
        ($record | ConvertTo-Json -Compress),
        (New-Object System.Text.UTF8Encoding($false))
    )

    return [pscustomobject] $record
}

function Get-NativeOwnedProcess {
    param(
        [Parameter(Mandatory = $true)]
        [pscustomobject] $Context,
        [Parameter(Mandatory = $true)]
        [ValidatePattern('^[a-z][a-z0-9-]{1,31}$')]
        [string] $Name,
        [int] $StartTimeToleranceSeconds = 5
    )

    $pidPath = Join-Path $Context.PidsRoot ($Name + '.json')
    if (-not (Test-Path -LiteralPath $pidPath -PathType Leaf)) {
        return $null
    }

    try {
        $record = Get-Content -Raw -LiteralPath $pidPath | ConvertFrom-Json
        $process = Get-Process -Id ([int] $record.pid) -ErrorAction Stop
        $expected = ConvertFrom-NativeProcessStartTime -Value $record.startedAtUtc
        $actual = $process.StartTime.ToUniversalTime()
        if ([Math]::Abs(($actual - $expected).TotalSeconds) -gt $StartTimeToleranceSeconds) {
            return $null
        }
        if ($process.ProcessName -notin @('uvx', 'uv', 'cmd', 'node', 'silo', 'python', 'python3')) {
            return $null
        }
        return [pscustomobject]@{
            Record = $record
            Process = $process
        }
    } catch {
        return $null
    }
}

function Stop-NativeOwnedProcess {
    param(
        [Parameter(Mandatory = $true)]
        [pscustomobject] $Context,
        [Parameter(Mandatory = $true)]
        [ValidatePattern('^[a-z][a-z0-9-]{1,31}$')]
        [string] $Name
    )

    $owned = Get-NativeOwnedProcess -Context $Context -Name $Name
    if (-not $owned) {
        Remove-NativeStalePid -Context $Context -Name $Name | Out-Null
        return $false
    }

    $rootId = [int] $owned.Process.Id
    $all = @(Get-CimInstance Win32_Process | Select-Object ProcessId, ParentProcessId)
    $children = New-Object 'System.Collections.Generic.List[int]'
    function Add-NativeDescendantProcessIds([int] $ParentId) {
        foreach ($child in $all) {
            if ([int] $child.ParentProcessId -eq $ParentId) {
                Add-NativeDescendantProcessIds ([int] $child.ProcessId)
                $children.Add([int] $child.ProcessId)
            }
        }
    }
    Add-NativeDescendantProcessIds $rootId
    foreach ($processId in $children) {
        Stop-Process -Id $processId -Force -ErrorAction SilentlyContinue
    }
    Stop-Process -Id $rootId -Force -ErrorAction SilentlyContinue
    Start-Sleep -Milliseconds 500
    Remove-NativeStalePid -Context $Context -Name $Name | Out-Null
    return $true
}

function Remove-NativeStalePid {
    param(
        [Parameter(Mandatory = $true)]
        [pscustomobject] $Context,
        [Parameter(Mandatory = $true)]
        [ValidatePattern('^[a-z][a-z0-9-]{1,31}$')]
        [string] $Name
    )

    if (Get-NativeOwnedProcess -Context $Context -Name $Name) {
        return $false
    }

    $pidPath = Join-Path $Context.PidsRoot ($Name + '.json')
    if (Test-Path -LiteralPath $pidPath -PathType Leaf) {
        Remove-Item -LiteralPath $pidPath -Force
        return $true
    }
    return $false
}

function Start-NativeOspPanel {
    param(
        [Parameter(Mandatory = $true)]
        [pscustomobject] $Context,
        [int] $TimeoutSeconds = 30
    )

    $started = $false
    $existing = Get-Process -Name 'ospanel' -ErrorAction SilentlyContinue
    if (-not $existing) {
        $started = $true
        Initialize-NativeContext -Context $Context
        $infrastructureLogs = Join-Path $Context.LogsRoot 'infrastructure'
        New-Item -ItemType Directory -Path $infrastructureLogs -Force | Out-Null
        $currentEnvironment = @{}
        foreach ($item in @([Environment]::GetEnvironmentVariables('Process').GetEnumerator())) {
            $currentEnvironment[[string] $item.Key] = [string] $item.Value
        }
        $allowed = @{}
        foreach ($name in $script:AllowedEnvironmentNames) {
            $allowed[$name] = $true
        }
        foreach ($key in $currentEnvironment.Keys) {
            if (-not $allowed.ContainsKey($key)) {
                [Environment]::SetEnvironmentVariable($key, $null, 'Process')
            }
        }
        try {
            Start-Process `
                -FilePath 'C:\OSPanel\bin\ospanel.exe' `
                -WorkingDirectory 'C:\OSPanel\bin' `
                -WindowStyle Hidden `
                -RedirectStandardOutput (Join-Path $infrastructureLogs 'ospanel.out.log') `
                -RedirectStandardError (Join-Path $infrastructureLogs 'ospanel.err.log') | Out-Null
        } finally {
            foreach ($key in $currentEnvironment.Keys) {
                [Environment]::SetEnvironmentVariable($key, $currentEnvironment[$key], 'Process')
            }
        }
    }

    if (-not (Wait-NativeTcpEndpoint -HostName '127.0.1.0' -Port 80 -TimeoutSeconds $TimeoutSeconds)) {
        throw 'OSPanel did not become ready on 127.0.1.0:80.'
    }
    return $started
}

function Get-NativeOspModuleReadiness {
    param(
        [Parameter(Mandatory = $true)]
        [pscustomobject] $Module,
        [scriptblock] $ListenerProvider,
        [scriptblock] $ProcessProvider
    )

    if (-not $ListenerProvider) {
        $ListenerProvider = {
            param([int] $Port)
            @(Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue)
        }
    }
    if (-not $ProcessProvider) {
        $ProcessProvider = {
            param([int] $ProcessId)
            Get-Process -Id $ProcessId -ErrorAction Stop
        }
    }
    try {
        $listeners = @(& $ListenerProvider ([int] $Module.Port))
    } catch {
        return [pscustomobject]@{ Status = 'Unavailable'; Reason = 'listener-query-failed' }
    }
    $exactListeners = @($listeners | Where-Object {
        $address = [string] $_.LocalAddress
        $address -eq [string] $Module.HostName
    })
    # Exact-host ownership is authoritative. Wildcard listeners on the same port
    # can belong to WSL/Docker and must not invalidate a healthy exact-host module.
    $matchingListeners = @(
        if ($exactListeners.Count -gt 0) {
            $exactListeners
        } else {
            $listeners | Where-Object {
                $address = [string] $_.LocalAddress
                $address -in @('0.0.0.0', '::')
            }
        }
    )
    if ($matchingListeners.Count -eq 0) {
        return [pscustomobject]@{ Status = 'Unavailable'; Reason = 'no-listener' }
    }

    $foreignOwner = $false
    $identityUnavailable = $false
    $ownedListener = $false
    foreach ($listener in $matchingListeners) {
        $processId = 0
        try {
            $processId = [int] $listener.OwningProcess
        } catch {
            $identityUnavailable = $true
            continue
        }
        if ($processId -le 0) {
            $identityUnavailable = $true
            continue
        }

        $process = $null
        try {
            $process = & $ProcessProvider $processId
        } catch {
            $identityUnavailable = $true
            continue
        }
        if ($null -eq $process) {
            $identityUnavailable = $true
            continue
        }

        $processPath = $null
        try {
            $processPath = [string] $process.Path
        } catch {
            $identityUnavailable = $true
            continue
        }
        if ([string]::IsNullOrWhiteSpace($processPath)) {
            $identityUnavailable = $true
            continue
        }
        if ($processPath -notmatch [string] $Module.ProcessPathPattern) {
            $foreignOwner = $true
            continue
        }
        $ownedListener = $true
    }

    if ($foreignOwner) {
        return [pscustomobject]@{ Status = 'ForeignOwner'; Reason = 'listener-owned-by-unexpected-process' }
    }
    if ($identityUnavailable) {
        return [pscustomobject]@{ Status = 'IdentityUnavailable'; Reason = 'process-identity-unavailable' }
    }
    if ($ownedListener) {
        return [pscustomobject]@{ Status = 'Ready'; Reason = 'expected-process-listener' }
    }
    return [pscustomobject]@{ Status = 'Unavailable'; Reason = 'eligible-listener-not-ready' }
}

function Get-NativeOspCommandBase {
    $commandLine = Get-Content -LiteralPath 'C:\OSPanel\bin\osp.bat' | Where-Object {
        $_ -match 'ospurl\.exe"\s+-f\s+(http://127\.0\.1\.0:\d+/api/cmd/[^"\s]+/all)'
    } | Select-Object -First 1
    if ($commandLine -and $commandLine -match '(http://127\.0\.1\.0:\d+/api/cmd/[^"\s]+)/all') {
        return $Matches[1]
    }
    throw 'Could not discover the local OSPanel command endpoint.'
}

function Invoke-NativeOspModuleStartRequest {
    param(
        [Parameter(Mandatory = $true)]
        [pscustomobject] $Module,
        [Parameter(Mandatory = $true)]
        [hashtable] $RequestContext,
        [int] $TimeoutSeconds
    )

    if (-not $RequestContext.ContainsKey('CommandBase')) {
        $RequestContext.CommandBase = Get-NativeOspCommandBase
    }
    return Invoke-NativeCapturedProcess `
        -FilePath 'C:\OSPanel\system\bin\ospurl.exe' `
        -ArgumentList @('-f', ($RequestContext.CommandBase + '/on/' + $Module.Name + '/')) `
        -WorkingDirectory 'C:\OSPanel\bin' `
        -TimeoutSeconds $TimeoutSeconds
}

function Invoke-NativeOspModuleReconciliation {
    param(
        [Parameter(Mandatory = $true)]
        [pscustomobject] $Module,
        [bool] $PanelWasStarted,
        [int] $AutoStartGraceSeconds = 5,
        [int] $TimeoutSeconds = 20,
        [int] $PollMilliseconds = 500,
        [scriptblock] $GetState,
        [scriptblock] $RequestStart,
        [scriptblock] $Delay
    )

    if (-not $GetState) {
        $GetState = {
            param([pscustomobject] $CurrentModule)
            Get-NativeOspModuleReadiness -Module $CurrentModule
        }
    }
    if (-not $Delay) {
        $Delay = {
            param([int] $Milliseconds)
            Start-Sleep -Milliseconds $Milliseconds
        }
    }
    if (-not $RequestStart) {
        throw 'OSPanel module reconciliation requires a start request handler.'
    }

    $state = & $GetState $Module
    if ($state.Status -eq 'Ready') {
        return
    }
    if ($state.Status -eq 'ForeignOwner') {
        throw ("OSPanel module {0} was not accepted because {1}:{2} is owned by an unexpected process. No start request was sent." -f $Module.Name, $Module.HostName, $Module.Port)
    }
    if ($state.Status -eq 'IdentityUnavailable') {
        throw ("OSPanel module {0} was not accepted because the owner of {1}:{2} could not be verified. No start request was sent." -f $Module.Name, $Module.HostName, $Module.Port)
    }

    if ($PanelWasStarted -and $AutoStartGraceSeconds -gt 0) {
        $graceDeadline = [DateTime]::UtcNow.AddSeconds($AutoStartGraceSeconds)
        do {
            & $Delay $PollMilliseconds
            $state = & $GetState $Module
            if ($state.Status -eq 'Ready') {
                return
            }
            if ($state.Status -eq 'ForeignOwner') {
                throw ("OSPanel module {0} was not accepted because {1}:{2} is owned by an unexpected process. No start request was sent." -f $Module.Name, $Module.HostName, $Module.Port)
            }
            if ($state.Status -eq 'IdentityUnavailable') {
                throw ("OSPanel module {0} was not accepted because the owner of {1}:{2} could not be verified. No start request was sent." -f $Module.Name, $Module.HostName, $Module.Port)
            }
        } while ([DateTime]::UtcNow -lt $graceDeadline)
    }

    $requestFailed = $false
    $requestExitCode = $null
    try {
        $requestResult = & $RequestStart $Module
        if ($null -ne $requestResult -and $requestResult.PSObject.Properties['ExitCode']) {
            $requestExitCode = [int] $requestResult.ExitCode
        }
    } catch {
        $requestFailed = $true
    }

    $deadline = [DateTime]::UtcNow.AddSeconds([Math]::Max(0, $TimeoutSeconds))
    do {
        $state = & $GetState $Module
        if ($state.Status -eq 'Ready') {
            return
        }
        if ($state.Status -eq 'ForeignOwner') {
            throw ("OSPanel module {0} was not accepted because {1}:{2} is owned by an unexpected process." -f $Module.Name, $Module.HostName, $Module.Port)
        }
        if ($state.Status -eq 'IdentityUnavailable') {
            throw ("OSPanel module {0} was not accepted because the owner of {1}:{2} could not be verified." -f $Module.Name, $Module.HostName, $Module.Port)
        }
        if ([DateTime]::UtcNow -ge $deadline) {
            break
        }
        & $Delay $PollMilliseconds
    } while ($true)

    if ($requestFailed) {
        $requestDetail = 'the start request failed'
    } elseif ($null -eq $requestExitCode) {
        $requestDetail = 'the start request returned no exit code'
    } elseif ($requestExitCode -ne 0) {
        $requestDetail = ("the start request exited with code {0}" -f $requestExitCode)
    } else {
        $requestDetail = 'the start request completed'
    }
    throw ("OSPanel module {0} did not become ready within {1} seconds ({2}). Check its status in OSPanel; raw start output was omitted." -f $Module.Name, $TimeoutSeconds, $requestDetail)
}

function Enable-NativeOspServices {
    param(
        [Parameter(Mandatory = $true)]
        [pscustomobject] $Context,
        [int] $TimeoutSeconds = 60,
        [int] $AutoStartGraceSeconds = 5
    )

    $panelWasStarted = [bool] (Start-NativeOspPanel -Context $Context -TimeoutSeconds 30)
    $moduleTimeoutSeconds = $TimeoutSeconds
    $requestContext = @{}
    $startRequestFunction = ${function:Invoke-NativeOspModuleStartRequest}
    $requestStart = {
        param([pscustomobject] $Module)
        & $startRequestFunction `
            -Module $Module `
            -RequestContext $requestContext `
            -TimeoutSeconds $moduleTimeoutSeconds
    }.GetNewClosure()
    $modules = @(
        [pscustomobject]@{
            Name = 'PostgreSQL-15'
            HostName = '127.0.1.19'
            Port = 5432
            ProcessPathPattern = '\\PostgreSQL-15\\bin\\postgres\.exe$'
        },
        [pscustomobject]@{
            Name = 'Redis'
            HostName = '127.0.1.25'
            Port = 6379
            ProcessPathPattern = '\\Redis\\redis-server\.exe$'
        },
        [pscustomobject]@{
            Name = 'RabbitMQ-3.13'
            HostName = '127.0.1.22'
            Port = 5672
            ProcessPathPattern = '\\ErlangOTP-26\\bin\\erl\.exe$'
        }
    )

    foreach ($module in $modules) {
        Invoke-NativeOspModuleReconciliation `
            -Module $module `
            -PanelWasStarted $panelWasStarted `
            -AutoStartGraceSeconds $AutoStartGraceSeconds `
            -TimeoutSeconds $moduleTimeoutSeconds `
            -RequestStart $requestStart
    }
}

function Restart-NativeOspModule {
    param(
        [Parameter(Mandatory = $true)]
        [pscustomobject] $Context,
        [Parameter(Mandatory = $true)]
        [ValidateSet('PostgreSQL-15', 'Redis', 'RabbitMQ-3.13')]
        [string] $Module,
        [Parameter(Mandatory = $true)]
        [int] $Port,
        [string] $HostName = '127.0.0.1',
        [int] $TimeoutSeconds = 60
    )

    Start-NativeOspPanel -Context $Context
    $commandLine = Get-Content -LiteralPath 'C:\OSPanel\bin\osp.bat' | Where-Object {
        $_ -match 'ospurl\.exe"\s+-f\s+(http://127\.0\.1\.0:\d+/api/cmd/[^"\s]+)/all'
    } | Select-Object -First 1
    if (-not $commandLine -or $commandLine -notmatch '(http://127\.0\.1\.0:\d+/api/cmd/[^"\s]+)/all') {
        throw 'Could not discover the local OSPanel command endpoint.'
    }
    $base = $Matches[1]
    $ospUrl = 'C:\OSPanel\system\bin\ospurl.exe'
    [void] (Invoke-NativeCapturedProcess -FilePath $ospUrl -ArgumentList @('-f', ($base + '/off/' + $Module + '/')) -WorkingDirectory 'C:\OSPanel\bin' -TimeoutSeconds 60)
    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    while ((Test-NativeTcpEndpoint -HostName $HostName -Port $Port -TimeoutMilliseconds 300) -and [DateTime]::UtcNow -lt $deadline) {
        Start-Sleep -Milliseconds 500
    }
    [void] (Invoke-NativeCapturedProcess -FilePath $ospUrl -ArgumentList @('-f', ($base + '/on/' + $Module + '/')) -WorkingDirectory 'C:\OSPanel\bin' -TimeoutSeconds 60)
    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    while (-not (Test-NativeTcpEndpoint -HostName $HostName -Port $Port -TimeoutMilliseconds 500) -and [DateTime]::UtcNow -lt $deadline) {
        Start-Sleep -Milliseconds 500
    }
    if (-not (Test-NativeTcpEndpoint -HostName $HostName -Port $Port)) {
        throw ("OSPanel module {0} did not return on port {1}." -f $Module, $Port)
    }
}

function New-NativeRandomSecret {
    param(
        [ValidateRange(12, 128)]
        [int] $ByteCount = 36
    )

    $bytes = New-Object byte[] $ByteCount
    $generator = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try {
        $generator.GetBytes($bytes)
    } finally {
        $generator.Dispose()
    }
    return [Convert]::ToBase64String($bytes).TrimEnd('=').Replace('+', '-').Replace('/', '_')
}

Export-ModuleMember -Function @(
    'Get-NativeRepoRoot',
    'New-NativeContext',
    'Get-NativeApiPort',
    'Get-NativeApiBaseUrl',
    'Get-NativeWebPort',
    'Get-NativeWebBaseUrl',
    'Test-NativeApiEndpointTarget',
    'Test-NativeStorageCorsOrigin',
    'Test-NativeOwnedProcessConfiguration',
    'ConvertFrom-NativeProcessStartTime',
    'Initialize-NativeContext',
    'Write-NativeStatus',
    'Assert-NativeCommand',
    'ConvertTo-NativeProcessArgument',
    'Invoke-NativeCapturedProcess',
    'Get-NativeToolCommands',
    'Get-NativeSiloRelease',
    'ConvertFrom-NativeChecksumManifest',
    'Resolve-NativeSiloChecksum',
    'Test-NativeTcpEndpoint',
    'Wait-NativeTcpEndpoint',
    'Test-NativeHttpEndpoint',
    'Start-NativeOwnedProcess',
    'Get-NativeOwnedProcess',
    'Stop-NativeOwnedProcess',
    'Remove-NativeStalePid',
    'Start-NativeOspPanel',
    'Enable-NativeOspServices',
    'Restart-NativeOspModule',
    'New-NativeRandomSecret'
)
