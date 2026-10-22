[CmdletBinding()]
param()

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

Import-Module (Join-Path $PSScriptRoot '..\NativeDev.Common.psm1') -Force
Import-Module (Join-Path $PSScriptRoot '..\NativeDev.Environment.psm1') -Force

$script:Passed = 0
$script:Failed = 0

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

function Assert-Throws {
    param(
        [Parameter(Mandatory = $true)]
        [scriptblock] $Action
    )

    try {
        & $Action
    } catch {
        return
    }
    throw 'Expected the action to throw.'
}

function Get-ThrowsMessage {
    param(
        [Parameter(Mandatory = $true)]
        [scriptblock] $Action
    )

    try {
        & $Action
    } catch {
        return $_.Exception.Message
    }
    throw 'Expected the action to throw.'
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

function Assert-Matches {
    param(
        [Parameter(Mandatory = $true)]
        [string] $Actual,
        [Parameter(Mandatory = $true)]
        [string] $Pattern
    )

    if ($Actual -notmatch $Pattern) {
        throw ("Expected '{0}' to match '{1}'." -f $Actual, $Pattern)
    }
}

function Assert-DoesNotMatch {
    param(
        [Parameter(Mandatory = $true)]
        [string] $Actual,
        [Parameter(Mandatory = $true)]
        [string] $Pattern
    )

    if ($Actual -match $Pattern) {
        throw ("Expected '{0}' not to match '{1}'." -f $Actual, $Pattern)
    }
}

Invoke-TestCase -Name 'native API and web endpoints are defined once for start and proxy configuration' -Test {
    Assert-Equal -Actual (Get-NativeApiPort) -Expected 8059
    Assert-Equal -Actual (Get-NativeApiBaseUrl) -Expected 'http://127.0.0.1:8059'
    Assert-Equal -Actual (Get-NativeWebPort) -Expected 3059
    Assert-Equal -Actual (Get-NativeWebBaseUrl) -Expected 'http://localhost:3059'
}

Invoke-TestCase -Name 'native API target accepts 8059 and rejects the old 8000 endpoint' -Test {
    $expected = Get-NativeApiBaseUrl
    Assert-True -Condition (Test-NativeApiEndpointTarget -Actual 'http://127.0.0.1:8059' -Expected $expected)
    Assert-Equal -Actual (Test-NativeApiEndpointTarget -Actual 'http://127.0.0.1:8000' -Expected $expected) -Expected $false
}

Invoke-TestCase -Name 'native web target accepts 3059 and rejects the generic 3000 endpoint' -Test {
    $expected = Get-NativeWebBaseUrl
    Assert-True -Condition (Test-NativeApiEndpointTarget -Actual 'http://localhost:3059' -Expected $expected)
    Assert-Equal -Actual (Test-NativeApiEndpointTarget -Actual 'http://localhost:3000' -Expected $expected) -Expected $false
}

Invoke-TestCase -Name 'storage CORS probe allows 3059 and rejects legacy 3000 origins' -Test {
    $provider = {
        param($Uri, $Headers)
        Assert-Equal -Actual $Uri -Expected 'http://127.0.0.1:9000/uploads/plane-cors-preflight'
        if ($Headers.Origin -eq 'http://localhost:3059') {
            return [pscustomobject]@{
                Headers = @{ 'Access-Control-Allow-Origin' = 'http://localhost:3059' }
            }
        }
        throw 'origin rejected'
    }

    Assert-True -Condition (Test-NativeStorageCorsOrigin `
        -Endpoint ([uri] 'http://127.0.0.1:9000') `
        -Bucket 'uploads' `
        -Origin 'http://localhost:3059' `
        -RequestProvider $provider)
    Assert-Equal -Actual (Test-NativeStorageCorsOrigin `
        -Endpoint ([uri] 'http://127.0.0.1:9000') `
        -Bucket 'uploads' `
        -Origin 'http://localhost:3000' `
        -RequestProvider $provider) -Expected $false
    Assert-Equal -Actual (Test-NativeStorageCorsOrigin `
        -Endpoint ([uri] 'http://127.0.0.1:9000') `
        -Bucket 'uploads' `
        -Origin 'http://127.0.0.1:3000' `
        -RequestProvider $provider) -Expected $false
}

Invoke-TestCase -Name 'legacy owned process records without a configuration tag are a safe mismatch' -Test {
    $legacyProcess = [pscustomobject]@{
        Record = [pscustomobject]@{
            pid = 1234
        }
    }

    Assert-Equal -Actual (Test-NativeOwnedProcessConfiguration -OwnedProcess $legacyProcess -ConfigurationTag 'native-api:http://127.0.0.1:8059') -Expected $false
    Assert-Equal -Actual (Test-NativeOwnedProcessConfiguration -OwnedProcess $null -ConfigurationTag '') -Expected $true
}

Invoke-TestCase -Name 'owned process start time preserves UTC for strings and parsed date objects' -Test {
    $iso = '2026-10-21T13:39:46.2311349Z'
    $expected = ([DateTimeOffset]::Parse(
        $iso,
        [System.Globalization.CultureInfo]::InvariantCulture,
        [System.Globalization.DateTimeStyles]::RoundtripKind
    )).UtcDateTime.ToString('o')

    $fromString = ConvertFrom-NativeProcessStartTime -Value $iso
    Assert-Equal -Actual $fromString.ToString('o') -Expected $expected

    $autoParsedRecord = ('{"startedAtUtc":"' + $iso + '"}') | ConvertFrom-Json
    $fromAutoParsed = ConvertFrom-NativeProcessStartTime -Value $autoParsedRecord.startedAtUtc
    Assert-Equal -Actual $fromAutoParsed.ToString('o') -Expected $expected

    $fromDateTimeOffset = ConvertFrom-NativeProcessStartTime -Value (
        [DateTimeOffset]::Parse('2026-10-21T18:39:46.2311349+05:00')
    )
    Assert-Equal -Actual $fromDateTimeOffset.ToString('o') -Expected $expected
}

Invoke-TestCase -Name 'environment builder uses the native API endpoint for live and web proxy' -Test {
    $tempRoot = Join-Path ([System.IO.Path]::GetTempPath()) ('plane-native-env-builder-' + [Guid]::NewGuid().ToString('N'))
    try {
        $tempContext = [pscustomobject]@{ RepoRoot = $tempRoot }
        foreach ($file in @(Get-NativeEnvironmentFiles -Context $tempContext)) {
            New-Item -ItemType Directory -Path (Split-Path -Parent $file.Path) -Force | Out-Null
            [System.IO.File]::WriteAllText(
                $file.ExamplePath,
                "# Test template`r`n",
                (New-Object System.Text.UTF8Encoding($false))
            )
        }

        [void] (New-NativeEnvironmentConfiguration `
            -Context $tempContext `
            -PostgresHost '127.0.0.1' `
            -PostgresPort 5432 `
            -PostgresUser 'plane_native' `
            -PostgresPassword 'test-password' `
            -PostgresDatabase 'plane' `
            -RedisHost '127.0.0.1' `
            -RedisPort 6379 `
            -RabbitHost '127.0.0.1' `
            -RabbitPort 5672 `
            -RabbitUser 'plane_native' `
            -RabbitPassword 'test-rabbit-password' `
            -RabbitVHost 'plane-native')

        $web = Read-NativeEnvironmentFile -Path (Join-Path $tempRoot 'apps\web\.env')
        $admin = Read-NativeEnvironmentFile -Path (Join-Path $tempRoot 'apps\admin\.env')
        $space = Read-NativeEnvironmentFile -Path (Join-Path $tempRoot 'apps\space\.env')
        $api = Read-NativeEnvironmentFile -Path (Join-Path $tempRoot 'apps\api\.env')
        $live = Read-NativeEnvironmentFile -Path (Join-Path $tempRoot 'apps\live\.env')
        $root = Read-NativeEnvironmentFile -Path (Join-Path $tempRoot '.env')
        Assert-Equal -Actual $web['NATIVE_API_PROXY_TARGET'] -Expected 'http://127.0.0.1:8059'
        Assert-Equal -Actual $web['VITE_API_BASE_URL'] -Expected 'http://localhost:3059'
        Assert-Equal -Actual $web['VITE_WEB_BASE_URL'] -Expected 'http://localhost:3059'
        Assert-Equal -Actual $admin['VITE_API_BASE_URL'] -Expected 'http://localhost:3059'
        Assert-Equal -Actual $space['VITE_WEB_BASE_URL'] -Expected 'http://localhost:3059'
        Assert-Equal -Actual $api['WEB_URL'] -Expected 'http://localhost:3059'
        Assert-Equal -Actual $api['APP_BASE_URL'] -Expected 'http://localhost:3059'
        Assert-Equal -Actual $live['API_BASE_URL'] -Expected 'http://127.0.0.1:8059'
        Assert-Matches -Actual $root['CORS_ALLOWED_ORIGINS'] -Pattern 'http://localhost:3059'
        Assert-Matches -Actual $api['CORS_ALLOWED_ORIGINS'] -Pattern 'http://localhost:3059'
        Assert-Matches -Actual $live['CORS_ALLOWED_ORIGINS'] -Pattern 'http://localhost:3059'
    } finally {
        if (Test-Path -LiteralPath $tempRoot -PathType Container) {
            Remove-Item -LiteralPath $tempRoot -Recurse -Force
        }
    }
}

Invoke-TestCase -Name 'managed env endpoint update preserves unrelated credentials' -Test {
    $tempRoot = Join-Path ([System.IO.Path]::GetTempPath()) ('plane-native-env-update-' + [Guid]::NewGuid().ToString('N'))
    try {
        $tempContext = [pscustomobject]@{ RepoRoot = $tempRoot }
        $files = Get-NativeEnvironmentFiles -Context $tempContext
        foreach ($file in $files) {
            New-Item -ItemType Directory -Path (Split-Path -Parent $file.Path) -Force | Out-Null
        }
        $managedHeader = '# Generated by dev/setup-native.ps1. Existing files are never replaced.'
        $encoding = New-Object System.Text.UTF8Encoding($false)
        [System.IO.File]::WriteAllLines(
            (Join-Path $tempRoot '.env'),
            @(
                $managedHeader,
                'CORS_ALLOWED_ORIGINS="http://localhost:3000,http://127.0.0.1:3000,http://localhost:3001,http://localhost:3002,http://localhost:3100"',
                'ROOT_SETTING="root-test-credential"'
            ),
            $encoding
        )
        [System.IO.File]::WriteAllLines(
            (Join-Path $tempRoot 'apps\api\.env'),
            @(
                $managedHeader,
                'WEB_URL="http://localhost:3000"',
                'APP_BASE_URL="http://localhost:3000"',
                'CORS_ALLOWED_ORIGINS="http://localhost:3000,http://127.0.0.1:3000,http://localhost:3001,http://localhost:3002,http://localhost:3100"',
                'SECRET_KEY="api-test-credential"'
            ),
            $encoding
        )
        [System.IO.File]::WriteAllLines(
            (Join-Path $tempRoot 'apps\web\.env'),
            @(
                $managedHeader,
                'VITE_API_BASE_URL="http://localhost:3000"',
                'VITE_WEB_BASE_URL="http://localhost:3000"',
                'NATIVE_API_PROXY_TARGET="http://127.0.0.1:8000"',
                'SECRET_KEY="web-test-credential"'
            ),
            $encoding
        )
        [System.IO.File]::WriteAllLines(
            (Join-Path $tempRoot 'apps\admin\.env'),
            @(
                $managedHeader,
                'VITE_API_BASE_URL="http://localhost:3000"',
                'VITE_WEB_BASE_URL="http://localhost:3000"',
                'ADMIN_SETTING="admin-test-credential"'
            ),
            $encoding
        )
        [System.IO.File]::WriteAllLines(
            (Join-Path $tempRoot 'apps\space\.env'),
            @(
                $managedHeader,
                'VITE_API_BASE_URL="http://localhost:3000"',
                'VITE_WEB_BASE_URL="http://localhost:3000"',
                'SPACE_SETTING="space-test-credential"'
            ),
            $encoding
        )
        [System.IO.File]::WriteAllLines(
            (Join-Path $tempRoot 'apps\live\.env'),
            @(
                $managedHeader,
                'API_BASE_URL="http://127.0.0.1:8000"',
                'CORS_ALLOWED_ORIGINS="http://localhost:3000,http://127.0.0.1:3000,http://localhost:3001,http://localhost:3002,http://localhost:3100"',
                'SECRET_KEY="live-test-credential"'
            ),
            $encoding
        )

        $result = Update-NativeApiEndpointConfiguration -Context $tempContext
        $root = Read-NativeEnvironmentFile -Path (Join-Path $tempRoot '.env')
        $api = Read-NativeEnvironmentFile -Path (Join-Path $tempRoot 'apps\api\.env')
        $web = Read-NativeEnvironmentFile -Path (Join-Path $tempRoot 'apps\web\.env')
        $admin = Read-NativeEnvironmentFile -Path (Join-Path $tempRoot 'apps\admin\.env')
        $space = Read-NativeEnvironmentFile -Path (Join-Path $tempRoot 'apps\space\.env')
        $live = Read-NativeEnvironmentFile -Path (Join-Path $tempRoot 'apps\live\.env')

        Assert-Equal -Actual $result.Updated.Count -Expected 6
        Assert-Equal -Actual $web['NATIVE_API_PROXY_TARGET'] -Expected 'http://127.0.0.1:8059'
        Assert-Equal -Actual $live['API_BASE_URL'] -Expected 'http://127.0.0.1:8059'
        Assert-Equal -Actual $web['VITE_API_BASE_URL'] -Expected 'http://localhost:3059'
        Assert-Equal -Actual $web['VITE_WEB_BASE_URL'] -Expected 'http://localhost:3059'
        Assert-Equal -Actual $admin['VITE_API_BASE_URL'] -Expected 'http://localhost:3059'
        Assert-Equal -Actual $space['VITE_WEB_BASE_URL'] -Expected 'http://localhost:3059'
        Assert-Equal -Actual $api['WEB_URL'] -Expected 'http://localhost:3059'
        Assert-Equal -Actual $api['APP_BASE_URL'] -Expected 'http://localhost:3059'
        Assert-Matches -Actual $root['CORS_ALLOWED_ORIGINS'] -Pattern 'http://localhost:3059'
        Assert-Matches -Actual $api['CORS_ALLOWED_ORIGINS'] -Pattern 'http://localhost:3059'
        Assert-Matches -Actual $live['CORS_ALLOWED_ORIGINS'] -Pattern 'http://localhost:3059'
        Assert-DoesNotMatch -Actual $root['CORS_ALLOWED_ORIGINS'] -Pattern 'http://localhost:3000'
        Assert-DoesNotMatch -Actual $root['CORS_ALLOWED_ORIGINS'] -Pattern 'http://127.0.0.1:3000'
        Assert-DoesNotMatch -Actual $api['CORS_ALLOWED_ORIGINS'] -Pattern 'http://localhost:3000'
        Assert-DoesNotMatch -Actual $api['CORS_ALLOWED_ORIGINS'] -Pattern 'http://127.0.0.1:3000'
        Assert-DoesNotMatch -Actual $live['CORS_ALLOWED_ORIGINS'] -Pattern 'http://localhost:3000'
        Assert-DoesNotMatch -Actual $live['CORS_ALLOWED_ORIGINS'] -Pattern 'http://127.0.0.1:3000'
        Assert-Equal -Actual $root['ROOT_SETTING'] -Expected 'root-test-credential'
        Assert-Equal -Actual $api['SECRET_KEY'] -Expected 'api-test-credential'
        Assert-Equal -Actual $web['SECRET_KEY'] -Expected 'web-test-credential'
        Assert-Equal -Actual $admin['ADMIN_SETTING'] -Expected 'admin-test-credential'
        Assert-Equal -Actual $space['SPACE_SETTING'] -Expected 'space-test-credential'
        Assert-Equal -Actual $live['SECRET_KEY'] -Expected 'live-test-credential'
    } finally {
        if (Test-Path -LiteralPath $tempRoot -PathType Container) {
            Remove-Item -LiteralPath $tempRoot -Recurse -Force
        }
    }
}

Invoke-TestCase -Name 'checksum manifest selects the pinned archive' -Test {
    $hash = ('a' * 64)
    $manifest = @(
        ('{0}  other.tar.gz' -f ('b' * 64)),
        ('{0} *silo_windows_amd64.tar.gz' -f $hash)
    ) -join "`n"
    $actual = ConvertFrom-NativeChecksumManifest `
        -Manifest $manifest `
        -ArchiveName 'silo_windows_amd64.tar.gz'
    Assert-Equal -Actual $actual -Expected $hash
}

Invoke-TestCase -Name 'checksum manifest supports SHA256 format' -Test {
    $hash = ('c' * 64)
    $manifest = ('SHA256 (silo_windows_amd64.tar.gz) = {0}' -f $hash)
    $actual = ConvertFrom-NativeChecksumManifest `
        -Manifest $manifest `
        -ArchiveName 'silo_windows_amd64.tar.gz'
    Assert-Equal -Actual $actual -Expected $hash
}

Invoke-TestCase -Name 'checksum manifest fails closed when missing' -Test {
    Assert-Throws {
        ConvertFrom-NativeChecksumManifest `
            -Manifest ('{0}  other.tar.gz' -f ('d' * 64)) `
            -ArchiveName 'silo_windows_amd64.tar.gz'
    }
}

Invoke-TestCase -Name 'instance readiness accepts JSON and credentialed CORS for onboarding state' -Test {
    $provider = {
        param($Uri, $Headers)
        [pscustomobject]@{
            StatusCode = 200
            Headers = @{
                'Content-Type' = 'application/json'
                'Access-Control-Allow-Origin' = 'http://localhost:3059'
                'Access-Control-Allow-Credentials' = 'true'
            }
            Content = (@{
                instance = @{ is_setup_done = $false; is_activated = $false }
                config = @{ is_self_managed = $true }
            } | ConvertTo-Json -Depth 4)
        }
    }
    $result = Test-NativeWebInstanceReadiness `
        -ApiBaseUrl 'http://127.0.0.1:8059' `
        -Origin 'http://localhost:3059' `
        -RequestProvider $provider
    Assert-True -Condition $result.Ok
}

Invoke-TestCase -Name 'instance readiness rejects HTTP 500' -Test {
    $provider = {
        param($Uri, $Headers)
        [pscustomobject]@{ StatusCode = 500; Headers = @{}; Content = '{}' }
    }
    $result = Test-NativeWebInstanceReadiness -ApiBaseUrl 'http://127.0.0.1:8059' -RequestProvider $provider
    Assert-Equal -Actual $result.Ok -Expected $false
    Assert-Equal -Actual $result.Reason -Expected 'http-status'
}

Invoke-TestCase -Name 'instance readiness rejects malformed JSON' -Test {
    $provider = {
        param($Uri, $Headers)
        [pscustomobject]@{
            StatusCode = 200
            Headers = @{
                'Content-Type' = 'application/json'
                'Access-Control-Allow-Origin' = 'http://localhost:3059'
                'Access-Control-Allow-Credentials' = 'true'
            }
            Content = '<html>not json</html>'
        }
    }
    $result = Test-NativeWebInstanceReadiness -ApiBaseUrl 'http://127.0.0.1:8059' -RequestProvider $provider
    Assert-Equal -Actual $result.Ok -Expected $false
    Assert-Equal -Actual $result.Reason -Expected 'malformed-json'
}

Invoke-TestCase -Name 'instance readiness rejects null instance/config without throwing' -Test {
    $provider = {
        param($Uri, $Headers)
        [pscustomobject]@{
            StatusCode = 200
            Headers = @{
                'Content-Type' = 'application/json'
                'Access-Control-Allow-Origin' = 'http://localhost:3059'
                'Access-Control-Allow-Credentials' = 'true'
            }
            Content = '{"instance":null,"config":null}'
        }
    }
    $result = Test-NativeWebInstanceReadiness -ApiBaseUrl 'http://127.0.0.1:8059' -RequestProvider $provider
    Assert-Equal -Actual $result.Ok -Expected $false
    Assert-Equal -Actual $result.Reason -Expected 'missing-instance-config'
}

Invoke-TestCase -Name 'instance readiness rejects invalid credentialed CORS' -Test {
    $provider = {
        param($Uri, $Headers)
        [pscustomobject]@{
            StatusCode = 200
            Headers = @{
                'Content-Type' = 'application/json'
                'Access-Control-Allow-Origin' = 'http://127.0.0.1:3059'
                'Access-Control-Allow-Credentials' = 'false'
            }
            Content = (@{
                instance = @{ is_setup_done = $true; is_activated = $true }
                config = @{ is_self_managed = $true }
            } | ConvertTo-Json -Depth 4)
        }
    }
    $result = Test-NativeWebInstanceReadiness -ApiBaseUrl 'http://127.0.0.1:8059' -RequestProvider $provider
    Assert-Equal -Actual $result.Ok -Expected $false
    Assert-Equal -Actual $result.Reason -Expected 'cors'
}

Invoke-TestCase -Name 'process arguments preserve spaces and quotes' -Test {
    $actual = ConvertTo-NativeProcessArgument -Value 'a "quoted" path'
    Assert-Equal -Actual $actual -Expected '"a \"quoted\" path"'
}

Invoke-TestCase -Name 'captured process returns output and exit code' -Test {
    $hostPath = (Get-Process -Id $PID).Path
    $result = Invoke-NativeCapturedProcess `
        -FilePath $hostPath `
        -ArgumentList @('-NoProfile', '-Command', 'Write-Output "captured"; [Console]::Error.WriteLine("stderr"); exit 7') `
        -WorkingDirectory $PSScriptRoot `
        -TimeoutSeconds 15
    Assert-Equal -Actual $result.ExitCode -Expected 7
    Assert-Equal -Actual $result.StandardOutput.Trim() -Expected 'captured'
    Assert-Equal -Actual $result.StandardError.Trim() -Expected 'stderr'
}

$nativeModule = Get-Module -Name 'NativeDev.Common'
$rabbitModule = [pscustomobject]@{
    Name = 'RabbitMQ-3.13'
    HostName = '127.0.1.22'
    Port = 5672
    ProcessPathPattern = '\\ErlangOTP-26\\bin\\erl\.exe$'
}

Invoke-TestCase -Name 'osp readiness sends no start request for an already ready module' -Test {
    $calls = [pscustomobject]@{ Starts = 0 }
    $getState = {
        param($ModuleSpec)
        [pscustomobject]@{ Status = 'Ready'; Reason = 'test-ready' }
    }
    $requestStart = {
        param($ModuleSpec)
        $calls.Starts = $calls.Starts + 1
        [pscustomobject]@{ ExitCode = 0 }
    }.GetNewClosure()

    & $nativeModule {
        param($ModuleSpec, $GetState, $RequestStart)
        Invoke-NativeOspModuleReconciliation `
            -Module $ModuleSpec `
            -PanelWasStarted $false `
            -AutoStartGraceSeconds 0 `
            -TimeoutSeconds 0 `
            -PollMilliseconds 1 `
            -GetState $GetState `
            -RequestStart $RequestStart
    } $rabbitModule $getState $requestStart

    Assert-Equal -Actual $calls.Starts -Expected 0
}

Invoke-TestCase -Name 'osp readiness waits briefly for delayed panel autostart' -Test {
    $calls = [pscustomobject]@{ Starts = 0; Checks = 0 }
    $getState = {
        param($ModuleSpec)
        $calls.Checks = $calls.Checks + 1
        if ($calls.Checks -ge 3) {
            return [pscustomobject]@{ Status = 'Ready'; Reason = 'autostart-complete' }
        }
        return [pscustomobject]@{ Status = 'Unavailable'; Reason = 'autostart-pending' }
    }.GetNewClosure()
    $requestStart = {
        param($ModuleSpec)
        $calls.Starts = $calls.Starts + 1
        [pscustomobject]@{ ExitCode = 0 }
    }.GetNewClosure()

    & $nativeModule {
        param($ModuleSpec, $GetState, $RequestStart)
        Invoke-NativeOspModuleReconciliation `
            -Module $ModuleSpec `
            -PanelWasStarted $true `
            -AutoStartGraceSeconds 5 `
            -TimeoutSeconds 0 `
            -PollMilliseconds 1 `
            -GetState $GetState `
            -RequestStart $RequestStart
    } $rabbitModule $getState $requestStart

    Assert-Equal -Actual $calls.Starts -Expected 0
    Assert-True -Condition ($calls.Checks -ge 3)
}

Invoke-TestCase -Name 'osp readiness reconciles a rejected start that becomes ready' -Test {
    $calls = [pscustomobject]@{ Starts = 0; Checks = 0 }
    $getState = {
        param($ModuleSpec)
        $calls.Checks = $calls.Checks + 1
        if ($calls.Checks -eq 1) {
            return [pscustomobject]@{ Status = 'Unavailable'; Reason = 'startup-pending' }
        }
        return [pscustomobject]@{ Status = 'Ready'; Reason = 'startup-complete' }
    }.GetNewClosure()
    $requestStart = {
        param($ModuleSpec)
        $calls.Starts = $calls.Starts + 1
        [pscustomobject]@{
            ExitCode = 1
            StandardError = 'token=should-not-be-rendered cookie=should-not-be-rendered'
        }
    }.GetNewClosure()

    & $nativeModule {
        param($ModuleSpec, $GetState, $RequestStart)
        Invoke-NativeOspModuleReconciliation `
            -Module $ModuleSpec `
            -PanelWasStarted $false `
            -AutoStartGraceSeconds 0 `
            -TimeoutSeconds 0 `
            -PollMilliseconds 1 `
            -GetState $GetState `
            -RequestStart $RequestStart
    } $rabbitModule $getState $requestStart

    Assert-Equal -Actual $calls.Starts -Expected 1
}

Invoke-TestCase -Name 'osp readiness reports a genuine failure after one start request' -Test {
    $calls = [pscustomobject]@{ Starts = 0 }
    $getState = {
        param($ModuleSpec)
        [pscustomobject]@{ Status = 'Unavailable'; Reason = 'startup-failed' }
    }
    $requestStart = {
        param($ModuleSpec)
        $calls.Starts = $calls.Starts + 1
        [pscustomobject]@{
            ExitCode = 1
            StandardOutput = 'AUTH_TOKEN=should-not-be-rendered'
            StandardError = 'Cookie: should-not-be-rendered'
        }
    }.GetNewClosure()

    $message = Get-ThrowsMessage {
        & $nativeModule {
            param($ModuleSpec, $GetState, $RequestStart)
            Invoke-NativeOspModuleReconciliation `
                -Module $ModuleSpec `
                -PanelWasStarted $false `
                -AutoStartGraceSeconds 0 `
                -TimeoutSeconds 0 `
                -PollMilliseconds 1 `
                -GetState $GetState `
                -RequestStart $RequestStart
        } $rabbitModule $getState $requestStart
    }

    Assert-Equal -Actual $calls.Starts -Expected 1
    Assert-Matches -Actual $message -Pattern 'RabbitMQ-3\.13 did not become ready'
    Assert-Matches -Actual $message -Pattern 'exited with code 1'
    Assert-DoesNotMatch -Actual $message -Pattern 'AUTH_TOKEN|Cookie|should-not-be-rendered'
}

Invoke-TestCase -Name 'osp readiness rejects a positively identified foreign Rabbit listener' -Test {
    $listenerProvider = {
        param($Port)
        [pscustomobject]@{ OwningProcess = 4242; LocalAddress = '127.0.1.22'; LocalPort = $Port }
    }
    $processProvider = {
        param($ProcessId)
        [pscustomobject]@{ Path = 'C:\OtherVendor\bin\erl.exe' }
    }

    $state = & $nativeModule {
        param($ModuleSpec, $ListenerProvider, $ProcessProvider)
        Get-NativeOspModuleReadiness `
            -Module $ModuleSpec `
            -ListenerProvider $ListenerProvider `
            -ProcessProvider $ProcessProvider
    } $rabbitModule $listenerProvider $processProvider

    Assert-Equal -Actual $state.Status -Expected 'ForeignOwner'

    $calls = [pscustomobject]@{ Starts = 0 }
    $getForeignState = {
        param($ModuleSpec)
        $state
    }.GetNewClosure()
    $requestStart = {
        param($ModuleSpec)
        $calls.Starts = $calls.Starts + 1
        [pscustomobject]@{ ExitCode = 0 }
    }.GetNewClosure()
    $message = Get-ThrowsMessage {
        & $nativeModule {
            param($ModuleSpec, $GetState, $RequestStart)
            Invoke-NativeOspModuleReconciliation `
                -Module $ModuleSpec `
                -PanelWasStarted $false `
                -AutoStartGraceSeconds 0 `
                -TimeoutSeconds 0 `
                -PollMilliseconds 1 `
                -GetState $GetState `
                -RequestStart $RequestStart
        } $rabbitModule $getForeignState $requestStart
    }

    Assert-Equal -Actual $calls.Starts -Expected 0
    Assert-Matches -Actual $message -Pattern 'owned by an unexpected process'
}

Invoke-TestCase -Name 'osp readiness scopes ownership to the expected module address' -Test {
    $postgresModule = [pscustomobject]@{
        Name = 'PostgreSQL-15'
        HostName = '127.0.1.19'
        Port = 5432
        ProcessPathPattern = '\\PostgreSQL-15\\bin\\postgres\.exe$'
    }
    $calls = [pscustomobject]@{ ProcessIds = @() }
    $listenerProvider = {
        param($Port)
        @(
            [pscustomobject]@{ OwningProcess = 4242; LocalAddress = '127.0.1.19'; LocalPort = $Port },
            [pscustomobject]@{ OwningProcess = 5353; LocalAddress = '127.0.1.20'; LocalPort = $Port }
        )
    }
    $processProvider = {
        param($ProcessId)
        $calls.ProcessIds = @($calls.ProcessIds) + [int] $ProcessId
        if ($ProcessId -eq 4242) {
            return [pscustomobject]@{ Path = 'C:\OSPanel\modules\PostgreSQL-15\bin\postgres.exe' }
        }
        return [pscustomobject]@{ Path = 'C:\OSPanel\modules\PostgreSQL-16\bin\postgres.exe' }
    }.GetNewClosure()

    $state = & $nativeModule {
        param($ModuleSpec, $ListenerProvider, $ProcessProvider)
        Get-NativeOspModuleReadiness `
            -Module $ModuleSpec `
            -ListenerProvider $ListenerProvider `
            -ProcessProvider $ProcessProvider
    } $postgresModule $listenerProvider $processProvider

    Assert-Equal -Actual $state.Status -Expected 'Ready'
    Assert-Equal -Actual $state.Reason -Expected 'expected-process-listener'
    Assert-Equal -Actual $calls.ProcessIds.Count -Expected 1
    Assert-Equal -Actual $calls.ProcessIds[0] -Expected 4242
}

Invoke-TestCase -Name 'osp readiness prefers an exact listener over unrelated wildcard owners' -Test {
    $postgresModule = [pscustomobject]@{
        Name = 'PostgreSQL-15'
        HostName = '127.0.1.19'
        Port = 5432
        ProcessPathPattern = '\\PostgreSQL-15\\bin\\postgres\.exe$'
    }
    $calls = [pscustomobject]@{ ProcessIds = @() }
    $listenerProvider = {
        param($Port)
        @(
            [pscustomobject]@{ OwningProcess = 4242; LocalAddress = '127.0.1.19'; LocalPort = $Port },
            [pscustomobject]@{ OwningProcess = 5353; LocalAddress = '::'; LocalPort = $Port },
            [pscustomobject]@{ OwningProcess = 6262; LocalAddress = '::1'; LocalPort = $Port }
        )
    }
    $processProvider = {
        param($ProcessId)
        $calls.ProcessIds = @($calls.ProcessIds) + [int] $ProcessId
        if ($ProcessId -eq 4242) {
            return [pscustomobject]@{ Path = 'C:\OSPanel\modules\PostgreSQL-15\bin\postgres.exe' }
        }
        return [pscustomobject]@{ Path = 'C:\Program Files\Docker\Docker\resources\com.docker.backend.exe' }
    }.GetNewClosure()

    $state = & $nativeModule {
        param($ModuleSpec, $ListenerProvider, $ProcessProvider)
        Get-NativeOspModuleReadiness `
            -Module $ModuleSpec `
            -ListenerProvider $ListenerProvider `
            -ProcessProvider $ProcessProvider
    } $postgresModule $listenerProvider $processProvider

    Assert-Equal -Actual $state.Status -Expected 'Ready'
    Assert-Equal -Actual $calls.ProcessIds.Count -Expected 1
    Assert-Equal -Actual $calls.ProcessIds[0] -Expected 4242
}

Invoke-TestCase -Name 'osp readiness fails closed when the eligible listener owner is unknown' -Test {
    $listenerProvider = {
        param($Port)
        [pscustomobject]@{ OwningProcess = 4242; LocalAddress = '127.0.1.22'; LocalPort = $Port }
    }
    $processProvider = {
        param($ProcessId)
        $process = New-Object psobject
        Add-Member -InputObject $process -MemberType ScriptProperty -Name Path -Value {
            throw (New-Object System.UnauthorizedAccessException 'access denied')
        }
        return $process
    }

    $state = & $nativeModule {
        param($ModuleSpec, $ListenerProvider, $ProcessProvider)
        Get-NativeOspModuleReadiness `
            -Module $ModuleSpec `
            -ListenerProvider $ListenerProvider `
            -ProcessProvider $ProcessProvider
    } $rabbitModule $listenerProvider $processProvider

    Assert-Equal -Actual $state.Status -Expected 'IdentityUnavailable'

    $calls = [pscustomobject]@{ Starts = 0 }
    $getUnknownState = {
        param($ModuleSpec)
        $state
    }.GetNewClosure()
    $requestStart = {
        param($ModuleSpec)
        $calls.Starts = $calls.Starts + 1
        [pscustomobject]@{ ExitCode = 0 }
    }.GetNewClosure()
    $message = Get-ThrowsMessage {
        & $nativeModule {
            param($ModuleSpec, $GetState, $RequestStart)
            Invoke-NativeOspModuleReconciliation `
                -Module $ModuleSpec `
                -PanelWasStarted $false `
                -AutoStartGraceSeconds 0 `
                -TimeoutSeconds 0 `
                -PollMilliseconds 1 `
                -GetState $GetState `
                -RequestStart $RequestStart
        } $rabbitModule $getUnknownState $requestStart
    }

    Assert-Equal -Actual $calls.Starts -Expected 0
    Assert-Matches -Actual $message -Pattern 'could not be verified'
}

Invoke-TestCase -Name 'osp enable dispatches module starts through the captured module function' -Test {
    $flow = [pscustomobject]@{
        Checks = @{}
        CapturedCalls = 0
        CommandBaseCalls = 0
        Modules = @()
        Timeouts = @()
    }
    $fakeStartPanel = {
        param($Context, [int] $TimeoutSeconds = 30)
        return $false
    }
    $fakeReadiness = {
        param([pscustomobject] $Module, [scriptblock] $ListenerProvider, [scriptblock] $ProcessProvider)
        $name = [string] $Module.Name
        if (-not $flow.Checks.ContainsKey($name)) {
            $flow.Checks[$name] = 0
        }
        $flow.Checks[$name] = [int] $flow.Checks[$name] + 1
        if ($flow.Checks[$name] -eq 1) {
            return [pscustomobject]@{ Status = 'Unavailable'; Reason = 'test-start-required' }
        }
        return [pscustomobject]@{ Status = 'Ready'; Reason = 'test-start-complete' }
    }.GetNewClosure()
    $fakeCommandBase = {
        $flow.CommandBaseCalls = $flow.CommandBaseCalls + 1
        return 'http://127.0.1.0:12345/api/cmd/redacted'
    }.GetNewClosure()
    $fakeCapturedProcess = {
        param($FilePath, [string[]] $ArgumentList, $WorkingDirectory, [int] $TimeoutSeconds)
        $flow.CapturedCalls = $flow.CapturedCalls + 1
        $flow.Timeouts = @($flow.Timeouts) + [int] $TimeoutSeconds
        $url = [string] $ArgumentList[1]
        if ($url -match '/on/([^/]+)/$') {
            $flow.Modules = @($flow.Modules) + $Matches[1]
        }
        return [pscustomobject]@{
            ExitCode = 0
            StandardOutput = ''
            StandardError = ''
        }
    }.GetNewClosure()

    $overrides = [ordered]@{
        'Start-NativeOspPanel' = $fakeStartPanel
        'Get-NativeOspModuleReadiness' = $fakeReadiness
        'Get-NativeOspCommandBase' = $fakeCommandBase
        'Invoke-NativeCapturedProcess' = $fakeCapturedProcess
    }
    $originals = @{}
    foreach ($name in $overrides.Keys) {
        $originals[$name] = & $nativeModule {
            param($FunctionName)
            (Get-Command -Name $FunctionName -CommandType Function -ErrorAction Stop).ScriptBlock
        } $name
        & $nativeModule {
            param($FunctionName, $Body)
            Set-Item -LiteralPath ('function:script:' + $FunctionName) -Value $Body
        } $name $overrides[$name]
    }

    try {
        Enable-NativeOspServices `
            -Context ([pscustomobject]@{}) `
            -TimeoutSeconds 60 `
            -AutoStartGraceSeconds 0

        Assert-Equal -Actual $flow.CapturedCalls -Expected 3
        Assert-Equal -Actual $flow.CommandBaseCalls -Expected 1
        foreach ($name in @('PostgreSQL-15', 'Redis', 'RabbitMQ-3.13')) {
            Assert-True -Condition ($flow.Modules -contains $name)
        }
        Assert-Equal -Actual $flow.Timeouts.Count -Expected 3
        foreach ($timeout in $flow.Timeouts) {
            Assert-Equal -Actual $timeout -Expected 60
        }
    } finally {
        foreach ($name in $originals.Keys) {
            & $nativeModule {
                param($FunctionName, $Body)
                Set-Item -LiteralPath ('function:script:' + $FunctionName) -Value $Body
            } $name $originals[$name]
        }
    }
}

Write-Host ("Tests: {0} passed, {1} failed" -f $script:Passed, $script:Failed)
if ($script:Failed -gt 0) {
    exit 1
}
