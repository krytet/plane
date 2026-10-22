[CmdletBinding()]
param()

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

$script:Passed = 0
$script:Failed = 0
$repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..\..')).Path
$sourceBat = Join-Path $repoRoot 'start-plane.bat'
$tempBase = [System.IO.Path]::GetTempPath()
$tempRoot = Join-Path $tempBase ('plane-start-plane-tests-' + [Guid]::NewGuid().ToString('N'))
$stubPowerShell = Join-Path $tempRoot 'powershell.cmd'
$stubBrowser = Join-Path $tempRoot 'browser.cmd'
$testBat = Join-Path $tempRoot 'start-plane.bat'
$originalEnvironment = @{}

function Write-Utf8File {
    param(
        [Parameter(Mandatory = $true)]
        [string] $Path,
        [AllowEmptyString()]
        [Parameter(Mandatory = $true)]
        [string] $Content
    )

    [System.IO.File]::WriteAllText(
        $Path,
        $Content,
        (New-Object System.Text.UTF8Encoding($false))
    )
}

function Set-TestEnvironment {
    param(
        [Parameter(Mandatory = $true)]
        [string] $Name,
        [AllowEmptyString()]
        [Parameter(Mandatory = $true)]
        [string] $Value
    )

    if (-not $originalEnvironment.ContainsKey($Name)) {
        $originalEnvironment[$Name] = [Environment]::GetEnvironmentVariable($Name, 'Process')
    }
    [Environment]::SetEnvironmentVariable($Name, $Value, 'Process')
}

function Restore-TestEnvironment {
    foreach ($name in $originalEnvironment.Keys) {
        [Environment]::SetEnvironmentVariable($name, $originalEnvironment[$name], 'Process')
    }
    $originalEnvironment.Clear()
}

function Assert-Equal {
    param(
        [Parameter(Mandatory = $true)]
        $Actual,
        [Parameter(Mandatory = $true)]
        $Expected
    )

    if ($Actual -ne $Expected) {
        throw ("Expected '{0}', got '{1}'." -f $Expected, $Actual)
    }
}

function Assert-True {
    param(
        [Parameter(Mandatory = $true)]
        [bool] $Condition,
        [string] $Message = 'Expected the condition to be true.'
    )

    if (-not $Condition) {
        throw $Message
    }
}

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

function Invoke-LauncherScenario {
    param(
        [int] $StartExit,
        [int] $CheckExit,
        [string] $Arguments = ''
    )

    $logPath = Join-Path $tempRoot ('calls-' + [Guid]::NewGuid().ToString('N') + '.log')
    Write-Utf8File -Path $logPath -Content ''
    Set-TestEnvironment -Name 'PLANE_TEST_LOG' -Value $logPath
    Set-TestEnvironment -Name 'PLANE_TEST_START_EXIT' -Value ([string] $StartExit)
    Set-TestEnvironment -Name 'PLANE_TEST_CHECK_EXIT' -Value ([string] $CheckExit)
    Set-TestEnvironment -Name 'PLANE_TEST_BROWSER' -Value $stubBrowser

    $commandLine = 'start-plane.bat --no-pause'
    if ($Arguments) {
        $commandLine += ' ' + $Arguments
    }

    Push-Location -LiteralPath $tempRoot
    try {
        $output = @(& $env:ComSpec /d /c $commandLine 2>&1)
        $exitCode = $LASTEXITCODE
    } finally {
        Pop-Location
    }

    $calls = if (Test-Path -LiteralPath $logPath -PathType Leaf) {
        @(Get-Content -LiteralPath $logPath)
    } else {
        @()
    }
    return [pscustomobject]@{
        ExitCode = $exitCode
        Output = $output
        Calls = $calls
    }
}

function Get-CallCount {
    param(
        [object[]] $Calls,
        [Parameter(Mandatory = $true)]
        [string] $Pattern
    )

    return @($Calls | Where-Object { $_ -match $Pattern }).Count
}

function Get-LogIndex {
    param(
        [object[]] $Calls,
        [Parameter(Mandatory = $true)]
        [string] $Pattern
    )

    for ($index = 0; $index -lt $Calls.Count; $index++) {
        if ([string] $Calls[$index] -match $Pattern) {
            return $index
        }
    }
    return -1
}

New-Item -ItemType Directory -Path (Join-Path $tempRoot 'dev') -Force | Out-Null
try {
    $source = [System.IO.File]::ReadAllText($sourceBat)
    $source = $source.Replace('powershell.exe', ('call "' + $stubPowerShell + '"'))
    $source = $source.Replace(
        'start "" "%PLANE_WEB_URL%"',
        'call "%PLANE_TEST_BROWSER%" "%PLANE_WEB_URL%"'
    )
    Write-Utf8File -Path $testBat -Content $source
    Write-Utf8File -Path (Join-Path $tempRoot 'dev\start-native.ps1') -Content ''
    Write-Utf8File -Path (Join-Path $tempRoot 'dev\check-native.ps1') -Content ''

    $powerShellStub = @(
        '@echo off',
        '>>"%PLANE_TEST_LOG%" echo POWERSHELL %*',
        'echo %* | findstr /I /C:"Get-NativeWebBaseUrl" >nul',
        'if not errorlevel 1 (',
        '    echo http://localhost:3059',
        '    exit /b 0',
        ')',
        'echo %* | findstr /I /C:"check-native.ps1" >nul',
        'if not errorlevel 1 exit /b %PLANE_TEST_CHECK_EXIT%',
        'exit /b %PLANE_TEST_START_EXIT%'
    ) -join "`r`n"
    Write-Utf8File -Path $stubPowerShell -Content ($powerShellStub + "`r`n")

    $browserStub = @(
        '@echo off',
        '>>"%PLANE_TEST_LOG%" echo BROWSER %*',
        'exit /b 0'
    ) -join "`r`n"
    Write-Utf8File -Path $stubBrowser -Content ($browserStub + "`r`n")

    Invoke-TestCase -Name 'start failure preserves its exit code and skips readiness and browser' -Test {
        $result = Invoke-LauncherScenario -StartExit 7 -CheckExit 0
        Assert-Equal -Actual $result.ExitCode -Expected 7
        Assert-Equal -Actual (Get-CallCount -Calls $result.Calls -Pattern '^POWERSHELL .*start-native\.ps1') -Expected 1
        Assert-Equal -Actual (Get-CallCount -Calls $result.Calls -Pattern '^POWERSHELL .*check-native\.ps1') -Expected 0
        Assert-Equal -Actual (Get-CallCount -Calls $result.Calls -Pattern '^BROWSER ') -Expected 0
    }

    Invoke-TestCase -Name 'readiness failure preserves its exit code and does not open the browser' -Test {
        $result = Invoke-LauncherScenario -StartExit 0 -CheckExit 9
        Assert-Equal -Actual $result.ExitCode -Expected 9
        Assert-Equal -Actual (Get-CallCount -Calls $result.Calls -Pattern '^POWERSHELL .*start-native\.ps1') -Expected 1
        Assert-Equal -Actual (Get-CallCount -Calls $result.Calls -Pattern '^POWERSHELL .*check-native\.ps1') -Expected 1
        Assert-Equal -Actual (Get-CallCount -Calls $result.Calls -Pattern '^BROWSER ') -Expected 0
    }

    Invoke-TestCase -Name 'successful start and readiness open the browser once in order' -Test {
        $result = Invoke-LauncherScenario -StartExit 0 -CheckExit 0
        Assert-Equal -Actual $result.ExitCode -Expected 0
        Assert-Equal -Actual (Get-CallCount -Calls $result.Calls -Pattern '^POWERSHELL .*start-native\.ps1') -Expected 1
        Assert-Equal -Actual (Get-CallCount -Calls $result.Calls -Pattern '^POWERSHELL .*check-native\.ps1') -Expected 1
        Assert-Equal -Actual (Get-CallCount -Calls $result.Calls -Pattern '^BROWSER ') -Expected 1
        Assert-Equal -Actual (Get-CallCount -Calls $result.Calls -Pattern '^BROWSER "?http://localhost:3059"?$') -Expected 1
        $startIndex = Get-LogIndex -Calls $result.Calls -Pattern '^POWERSHELL .*start-native\.ps1'
        $checkIndex = Get-LogIndex -Calls $result.Calls -Pattern '^POWERSHELL .*check-native\.ps1'
        $browserIndex = Get-LogIndex -Calls $result.Calls -Pattern '^BROWSER '
        Assert-True -Condition ($startIndex -ge 0 -and $startIndex -lt $checkIndex -and $checkIndex -lt $browserIndex)
    }

    Invoke-TestCase -Name 'no-browser keeps full readiness but suppresses the browser' -Test {
        $result = Invoke-LauncherScenario -StartExit 0 -CheckExit 0 -Arguments '--no-browser'
        Assert-Equal -Actual $result.ExitCode -Expected 0
        Assert-Equal -Actual (Get-CallCount -Calls $result.Calls -Pattern '^POWERSHELL .*start-native\.ps1') -Expected 1
        Assert-Equal -Actual (Get-CallCount -Calls $result.Calls -Pattern '^POWERSHELL .*check-native\.ps1') -Expected 1
        Assert-Equal -Actual (Get-CallCount -Calls $result.Calls -Pattern '^BROWSER ') -Expected 0
    }

    Invoke-TestCase -Name 'check mode runs only readiness and never opens the browser' -Test {
        $result = Invoke-LauncherScenario -StartExit 0 -CheckExit 0 -Arguments '--check'
        Assert-Equal -Actual $result.ExitCode -Expected 0
        Assert-Equal -Actual (Get-CallCount -Calls $result.Calls -Pattern '^POWERSHELL .*start-native\.ps1') -Expected 0
        Assert-Equal -Actual (Get-CallCount -Calls $result.Calls -Pattern '^POWERSHELL .*check-native\.ps1') -Expected 1
        Assert-Equal -Actual (Get-CallCount -Calls $result.Calls -Pattern '^BROWSER ') -Expected 0
    }

    Invoke-TestCase -Name 'check mode preserves readiness failure and never opens the browser' -Test {
        $result = Invoke-LauncherScenario -StartExit 0 -CheckExit 5 -Arguments '--check'
        Assert-Equal -Actual $result.ExitCode -Expected 5
        Assert-Equal -Actual (Get-CallCount -Calls $result.Calls -Pattern '^POWERSHELL .*start-native\.ps1') -Expected 0
        Assert-Equal -Actual (Get-CallCount -Calls $result.Calls -Pattern '^POWERSHELL .*check-native\.ps1') -Expected 1
        Assert-Equal -Actual (Get-CallCount -Calls $result.Calls -Pattern '^BROWSER ') -Expected 0
    }
} finally {
    Restore-TestEnvironment
    $resolvedTempBase = [System.IO.Path]::GetFullPath($tempBase).TrimEnd('\')
    $resolvedTempRoot = [System.IO.Path]::GetFullPath($tempRoot)
    if (
        (Test-Path -LiteralPath $resolvedTempRoot -PathType Container) -and
        ([System.IO.Path]::GetDirectoryName($resolvedTempRoot).TrimEnd('\') -eq $resolvedTempBase) -and
        ([System.IO.Path]::GetFileName($resolvedTempRoot) -like 'plane-start-plane-tests-*')
    ) {
        Remove-Item -LiteralPath $resolvedTempRoot -Recurse -Force
    }
}

Write-Host ("Tests: {0} passed, {1} failed" -f $script:Passed, $script:Failed)
if ($script:Failed -gt 0) {
    exit 1
}
