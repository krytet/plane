Set-StrictMode -Version 2.0

$script:EnvironmentFiles = @(
    [pscustomobject]@{ Name = 'root'; RelativePath = '.env'; Example = '.env.example' },
    [pscustomobject]@{ Name = 'api'; RelativePath = 'apps\api\.env'; Example = 'apps\api\.env.example' },
    [pscustomobject]@{ Name = 'web'; RelativePath = 'apps\web\.env'; Example = 'apps\web\.env.example' },
    [pscustomobject]@{ Name = 'admin'; RelativePath = 'apps\admin\.env'; Example = 'apps\admin\.env.example' },
    [pscustomobject]@{ Name = 'space'; RelativePath = 'apps\space\.env'; Example = 'apps\space\.env.example' },
    [pscustomobject]@{ Name = 'live'; RelativePath = 'apps\live\.env'; Example = 'apps\live\.env.example' }
)

function Get-NativeEnvironmentFiles {
    param(
        [Parameter(Mandatory = $true)]
        [pscustomobject] $Context
    )

    return @($script:EnvironmentFiles | ForEach-Object {
        [pscustomobject]@{
            Name = $_.Name
            Path = Join-Path $Context.RepoRoot $_.RelativePath
            ExamplePath = Join-Path $Context.RepoRoot $_.Example
        }
    })
}

function ConvertFrom-NativeEnvValue {
    param(
        [AllowEmptyString()]
        [string] $Value
    )

    $trimmed = $Value.Trim()
    if ($trimmed.Length -ge 2) {
        $first = $trimmed[0]
        $last = $trimmed[$trimmed.Length - 1]
        if (($first -eq '"' -and $last -eq '"') -or ($first -eq "'" -and $last -eq "'")) {
            return $trimmed.Substring(1, $trimmed.Length - 2)
        }
    }
    return ($trimmed -replace '\s+#.*$', '')
}

function Read-NativeEnvironmentFile {
    param(
        [Parameter(Mandatory = $true)]
        [string] $Path
    )

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw ("Environment file not found: {0}" -f $Path)
    }

    $values = @{}
    foreach ($line in @(Get-Content -LiteralPath $Path)) {
        if ($line -match '^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$') {
            $values[$Matches[1]] = ConvertFrom-NativeEnvValue -Value $Matches[2]
        }
    }
    return $values
}

function ConvertTo-NativeEnvFileValue {
    param(
        [AllowEmptyString()]
        [Parameter(Mandatory = $true)]
        [string] $Value
    )

    if ($Value -match "[\r\n`0]") {
        throw 'Environment values cannot contain line breaks or NUL characters.'
    }
    return '"' + $Value.Replace('\', '\\').Replace('"', '\"') + '"'
}

function Set-NativeEnvKeyValues {
    param(
        [AllowEmptyString()]
        [Parameter(Mandatory = $true)]
        [string[]] $Lines,
        [Parameter(Mandatory = $true)]
        [System.Collections.IDictionary] $Values
    )

    $output = New-Object System.Collections.Generic.List[string]
    $written = @{}

    foreach ($line in $Lines) {
        if ($line -match '^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$') {
            $key = $Matches[1]
            if ($Values.Contains($key)) {
                if (-not $written.ContainsKey($key)) {
                    $output.Add(('{0}={1}' -f $key, (ConvertTo-NativeEnvFileValue -Value ([string] $Values[$key]))))
                    $written[$key] = $true
                }
                continue
            }
        }
        $output.Add($line)
    }

    foreach ($key in $Values.Keys) {
        if (-not $written.ContainsKey($key)) {
            $output.Add(('{0}={1}' -f $key, (ConvertTo-NativeEnvFileValue -Value ([string] $Values[$key]))))
        }
    }
    return $output.ToArray()
}

function Read-NativeOptionalEnvValue {
    param(
        [Parameter(Mandatory = $true)]
        [string[]] $Paths,
        [Parameter(Mandatory = $true)]
        [string] $Key
    )

    foreach ($path in $Paths) {
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
            continue
        }
        $values = Read-NativeEnvironmentFile -Path $path
        if ($values.ContainsKey($Key) -and -not [string]::IsNullOrWhiteSpace([string] $values[$Key])) {
            return [string] $values[$Key]
        }
    }
    return $null
}

function Write-NativeEnvironmentFile {
    param(
        [Parameter(Mandatory = $true)]
        [string] $Path,
        [AllowEmptyString()]
        [Parameter(Mandatory = $true)]
        [string[]] $Lines
    )

    $directory = Split-Path -Parent $Path
    if (-not (Test-Path -LiteralPath $directory -PathType Container)) {
        New-Item -ItemType Directory -Path $directory -Force | Out-Null
    }

    $encoding = New-Object System.Text.UTF8Encoding($false)
    $stream = [System.IO.File]::Open($Path, [System.IO.FileMode]::CreateNew, [System.IO.FileAccess]::Write, [System.IO.FileShare]::None)
    try {
        $writer = New-Object System.IO.StreamWriter($stream, $encoding)
        try {
            foreach ($line in $Lines) {
                $writer.WriteLine($line)
            }
        } finally {
            $writer.Dispose()
        }
    } finally {
        $stream.Dispose()
    }
}

function Get-NativeRequiredEnvironmentKeys {
    param(
        [Parameter(Mandatory = $true)]
        [string] $Name
    )

    switch ($Name) {
        'root' {
            return @('POSTGRES_USER', 'POSTGRES_PASSWORD', 'POSTGRES_HOST', 'POSTGRES_PORT', 'POSTGRES_DB', 'REDIS_HOST', 'REDIS_PORT', 'RABBITMQ_HOST', 'RABBITMQ_PORT', 'AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY', 'AWS_S3_ENDPOINT_URL', 'USE_MINIO')
        }
        'api' {
            return @('SECRET_KEY', 'POSTGRES_USER', 'POSTGRES_PASSWORD', 'POSTGRES_HOST', 'POSTGRES_PORT', 'POSTGRES_DB', 'DATABASE_URL', 'REDIS_URL', 'RABBITMQ_VHOST', 'AMQP_URL', 'LIVE_SERVER_SECRET_KEY', 'AWS_REGION', 'AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY', 'AWS_S3_ENDPOINT_URL', 'AWS_S3_BUCKET_NAME', 'USE_MINIO', 'CORS_ALLOWED_ORIGINS')
        }
        'live' {
            return @('PORT', 'API_BASE_URL', 'CORS_ALLOWED_ORIGINS', 'LIVE_SERVER_SECRET_KEY', 'REDIS_URL')
        }
        default {
            return @('VITE_API_BASE_URL')
        }
    }
}

function New-NativeEnvironmentConfiguration {
    param(
        [Parameter(Mandatory = $true)]
        [pscustomobject] $Context,
        [Parameter(Mandatory = $true)]
        [string] $PostgresHost,
        [Parameter(Mandatory = $true)]
        [ValidateRange(1, 65535)]
        [int] $PostgresPort,
        [Parameter(Mandatory = $true)]
        [string] $PostgresUser,
        [AllowEmptyString()]
        [Parameter(Mandatory = $true)]
        [string] $PostgresPassword,
        [Parameter(Mandatory = $true)]
        [ValidatePattern('^[A-Za-z_][A-Za-z0-9_]*$')]
        [string] $PostgresDatabase,
        [Parameter(Mandatory = $true)]
        [string] $RedisHost,
        [Parameter(Mandatory = $true)]
        [ValidateRange(1, 65535)]
        [int] $RedisPort,
        [ValidateRange(0, 15)]
        [int] $RedisDatabase = 10,
        [AllowEmptyString()]
        [string] $RedisPassword = '',
        [Parameter(Mandatory = $true)]
        [string] $RabbitHost,
        [Parameter(Mandatory = $true)]
        [ValidateRange(1, 65535)]
        [int] $RabbitPort,
        [Parameter(Mandatory = $true)]
        [string] $RabbitUser,
        [Parameter(Mandatory = $true)]
        [string] $RabbitPassword,
        [Parameter(Mandatory = $true)]
        [ValidatePattern('^[A-Za-z0-9._-]+$')]
        [string] $RabbitVHost,
        [string] $S3Endpoint = 'http://127.0.0.1:9000',
        [string] $S3Bucket = 'uploads'
    )

    $files = Get-NativeEnvironmentFiles -Context $Context
    $existingPaths = @($files | Where-Object { Test-Path -LiteralPath $_.Path -PathType Leaf } | Select-Object -ExpandProperty Path)
    $apiPath = ($files | Where-Object { $_.Name -eq 'api' }).Path
    $livePath = ($files | Where-Object { $_.Name -eq 'live' }).Path
    $rootPath = ($files | Where-Object { $_.Name -eq 'root' }).Path
    $secretCandidates = @($apiPath, $livePath, $rootPath)

    $secretKey = Read-NativeOptionalEnvValue -Paths $secretCandidates -Key 'SECRET_KEY'
    if (-not $secretKey) {
        $secretKey = New-NativeRandomSecret
    }
    $liveSecret = Read-NativeOptionalEnvValue -Paths @($apiPath, $livePath, $rootPath) -Key 'LIVE_SERVER_SECRET_KEY'
    if (-not $liveSecret) {
        $liveSecret = New-NativeRandomSecret
    }
    $s3AccessKey = Read-NativeOptionalEnvValue -Paths @($apiPath, $rootPath) -Key 'AWS_ACCESS_KEY_ID'
    if (-not $s3AccessKey -or $s3AccessKey -in @('access-key', 'secret-key')) {
        $s3AccessKey = 'plane-local-' + (New-NativeRandomSecret -ByteCount 18)
    }
    $s3SecretKey = Read-NativeOptionalEnvValue -Paths @($apiPath, $rootPath) -Key 'AWS_SECRET_ACCESS_KEY'
    if (-not $s3SecretKey -or $s3SecretKey -in @('access-key', 'secret-key')) {
        $s3SecretKey = New-NativeRandomSecret -ByteCount 48
    }

    $escapedPostgresUser = [Uri]::EscapeDataString($PostgresUser)
    $escapedPostgresPassword = [Uri]::EscapeDataString($PostgresPassword)
    $databaseUrl = 'postgresql://{0}:{1}@{2}:{3}/{4}' -f $escapedPostgresUser, $escapedPostgresPassword, $PostgresHost, $PostgresPort, $PostgresDatabase
    $redisAuthority = $RedisHost + ':' + $RedisPort
    if ($RedisPassword) {
        $redisAuthority = ':' + [Uri]::EscapeDataString($RedisPassword) + '@' + $redisAuthority
    }
    $redisUrl = 'redis://' + $redisAuthority + '/' + $RedisDatabase
    $amqpUrl = 'amqp://{0}:{1}@{2}:{3}/{4}' -f `
        [Uri]::EscapeDataString($RabbitUser), `
        [Uri]::EscapeDataString($RabbitPassword), `
        $RabbitHost, `
        $RabbitPort, `
        [Uri]::EscapeDataString($RabbitVHost)

    $webBaseUrl = Get-NativeWebBaseUrl
    $frontendOrigins = '{0},http://localhost:3001,http://localhost:3002,http://localhost:3100' -f $webBaseUrl
    $commonValues = [ordered]@{
        POSTGRES_USER = $PostgresUser
        POSTGRES_PASSWORD = $PostgresPassword
        POSTGRES_HOST = $PostgresHost
        POSTGRES_PORT = [string] $PostgresPort
        POSTGRES_DB = $PostgresDatabase
        DATABASE_URL = $databaseUrl
        REDIS_HOST = $RedisHost
        REDIS_PORT = [string] $RedisPort
        REDIS_URL = $redisUrl
        RABBITMQ_HOST = $RabbitHost
        RABBITMQ_PORT = [string] $RabbitPort
        RABBITMQ_USER = $RabbitUser
        RABBITMQ_PASSWORD = $RabbitPassword
        RABBITMQ_VHOST = $RabbitVHost
        AMQP_URL = $amqpUrl
        AWS_REGION = 'us-east-1'
        AWS_ACCESS_KEY_ID = $s3AccessKey
        AWS_SECRET_ACCESS_KEY = $s3SecretKey
        AWS_S3_ENDPOINT_URL = $S3Endpoint
        AWS_S3_BUCKET_NAME = $S3Bucket
        USE_MINIO = '0'
        MINIO_ENDPOINT_SSL = '0'
        CORS_ALLOWED_ORIGINS = $frontendOrigins
    }

    $perFileValues = @{
        root = [ordered]@{}
        api = [ordered]@{
            SECRET_KEY = $secretKey
            DEBUG = '1'
            ENABLE_SIGNUP = '1'
            ENABLE_EMAIL_PASSWORD = '1'
            ENABLE_SMTP = '0'
            LIVE_SERVER_SECRET_KEY = $liveSecret
            WEB_URL = $webBaseUrl
            APP_BASE_URL = $webBaseUrl
            ADMIN_BASE_URL = 'http://localhost:3001'
            SPACE_BASE_URL = 'http://localhost:3002'
            LIVE_BASE_URL = 'http://localhost:3100'
            SIGNED_URL_EXPIRATION = '3600'
            FILE_SIZE_LIMIT = '5242880'
            ALLOWED_HOSTS = 'localhost,127.0.0.1'
        }
        web = [ordered]@{
            VITE_API_BASE_URL = $webBaseUrl
            NATIVE_API_PROXY_TARGET = (Get-NativeApiBaseUrl)
            VITE_WEB_BASE_URL = $webBaseUrl
            VITE_ADMIN_BASE_URL = 'http://localhost:3001'
            VITE_ADMIN_BASE_PATH = '/god-mode'
            VITE_SPACE_BASE_URL = 'http://localhost:3002'
            VITE_SPACE_BASE_PATH = '/spaces'
            VITE_LIVE_BASE_URL = 'http://localhost:3100'
            VITE_LIVE_BASE_PATH = '/live'
            HOST = '127.0.0.1'
        }
        admin = [ordered]@{
            VITE_API_BASE_URL = $webBaseUrl
            VITE_WEB_BASE_URL = $webBaseUrl
            VITE_ADMIN_BASE_URL = 'http://localhost:3001'
            VITE_ADMIN_BASE_PATH = '/god-mode'
            VITE_SPACE_BASE_URL = 'http://localhost:3002'
            VITE_SPACE_BASE_PATH = '/spaces'
            VITE_LIVE_BASE_URL = 'http://localhost:3100'
            VITE_LIVE_BASE_PATH = '/live'
            HOST = '127.0.0.1'
        }
        space = [ordered]@{
            VITE_API_BASE_URL = $webBaseUrl
            VITE_WEB_BASE_URL = $webBaseUrl
            VITE_ADMIN_BASE_URL = 'http://localhost:3001'
            VITE_ADMIN_BASE_PATH = '/god-mode'
            VITE_SPACE_BASE_URL = 'http://localhost:3002'
            VITE_SPACE_BASE_PATH = '/spaces'
            VITE_LIVE_BASE_URL = 'http://localhost:3100'
            VITE_LIVE_BASE_PATH = '/live'
            HOST = '127.0.0.1'
        }
        live = [ordered]@{
            PORT = '3100'
            API_BASE_URL = (Get-NativeApiBaseUrl)
            LIVE_BASE_PATH = '/live'
            LIVE_SERVER_SECRET_KEY = $liveSecret
            CORS_ALLOWED_ORIGINS = $frontendOrigins
            HOST = '127.0.0.1'
        }
    }

    $created = @()
    $preserved = @()
    foreach ($file in $files) {
        if (Test-Path -LiteralPath $file.Path -PathType Leaf) {
            $preserved += $file.Path
            continue
        }

        $template = @(Get-Content -LiteralPath $file.ExamplePath)
        $values = [ordered]@{}
        if ($file.Name -eq 'root' -or $file.Name -eq 'api') {
            foreach ($key in $commonValues.Keys) {
                $values[$key] = $commonValues[$key]
            }
        } elseif ($file.Name -eq 'live') {
            $values['REDIS_HOST'] = $RedisHost
            $values['REDIS_PORT'] = [string] $RedisPort
            $values['REDIS_URL'] = $redisUrl
        }
        foreach ($key in $perFileValues[$file.Name].Keys) {
            $values[$key] = $perFileValues[$file.Name][$key]
        }

        $lines = @(
            '# Generated by dev/setup-native.ps1. Existing files are never replaced.',
            '# Keep this file local. Values are environment-specific.'
        ) + @(Set-NativeEnvKeyValues -Lines $template -Values $values)
        Write-NativeEnvironmentFile -Path $file.Path -Lines $lines
        $created += $file.Path
    }

    return [pscustomobject]@{
        Created = $created
        Preserved = $preserved
        Paths = @($files | Select-Object -ExpandProperty Path)
    }
}

function Test-NativeEnvironmentConfiguration {
    param(
        [Parameter(Mandatory = $true)]
        [pscustomobject] $Context
    )

    $failures = @()
    foreach ($file in @(Get-NativeEnvironmentFiles -Context $Context)) {
        if (-not (Test-Path -LiteralPath $file.Path -PathType Leaf)) {
            $failures += ("{0}: missing" -f $file.Path)
            continue
        }
        $values = Read-NativeEnvironmentFile -Path $file.Path
        foreach ($key in @(Get-NativeRequiredEnvironmentKeys -Name $file.Name)) {
            $emptyIsValid = $key -eq 'POSTGRES_PASSWORD'
            if (
                -not $values.ContainsKey($key) -or
                (-not $emptyIsValid -and [string]::IsNullOrWhiteSpace([string] $values[$key]))
            ) {
                $failures += ("{0}: missing {1}" -f $file.Path, $key)
            }
        }
    }

    return [pscustomobject]@{
        IsValid = ($failures.Count -eq 0)
        Failures = $failures
    }
}

function Test-NativeLoopbackHost {
    param(
        [Parameter(Mandatory = $true)]
        [string] $HostName
    )

    if ([string]::Equals($HostName, 'localhost', [StringComparison]::OrdinalIgnoreCase)) {
        return $true
    }

    $address = $null
    if ([System.Net.IPAddress]::TryParse($HostName, [ref] $address)) {
        return [System.Net.IPAddress]::IsLoopback($address)
    }

    try {
        $addresses = @([System.Net.Dns]::GetHostAddresses($HostName))
        return $addresses.Count -gt 0 -and @($addresses | Where-Object { -not [System.Net.IPAddress]::IsLoopback($_) }).Count -eq 0
    } catch {
        return $false
    }
}

function Assert-NativeLocalEnvironment {
    param(
        [Parameter(Mandatory = $true)]
        [string] $ApiEnvPath
    )

    $values = Read-NativeEnvironmentFile -Path $ApiEnvPath
    $targets = @()

    if ($values.ContainsKey('DATABASE_URL') -and $values['DATABASE_URL']) {
        $targets += [pscustomobject]@{ Name = 'DATABASE_URL'; Uri = [Uri] $values['DATABASE_URL'] }
    } else {
        $targets += [pscustomobject]@{ Name = 'POSTGRES_HOST'; Host = [string] $values['POSTGRES_HOST']; Port = [string] $values['POSTGRES_PORT'] }
    }

    if ($values.ContainsKey('REDIS_URL') -and $values['REDIS_URL']) {
        $targets += [pscustomobject]@{ Name = 'REDIS_URL'; Uri = [Uri] $values['REDIS_URL'] }
    } else {
        $targets += [pscustomobject]@{ Name = 'REDIS_HOST'; Host = [string] $values['REDIS_HOST']; Port = [string] $values['REDIS_PORT'] }
    }

    if ($values.ContainsKey('AMQP_URL') -and $values['AMQP_URL']) {
        $targets += [pscustomobject]@{ Name = 'AMQP_URL'; Uri = [Uri] $values['AMQP_URL'] }
    } else {
        $targets += [pscustomobject]@{ Name = 'RABBITMQ_HOST'; Host = [string] $values['RABBITMQ_HOST']; Port = [string] $values['RABBITMQ_PORT'] }
    }

    if ($values.ContainsKey('AWS_S3_ENDPOINT_URL') -and $values['AWS_S3_ENDPOINT_URL']) {
        $targets += [pscustomobject]@{ Name = 'AWS_S3_ENDPOINT_URL'; Uri = [Uri] $values['AWS_S3_ENDPOINT_URL'] }
    }

    $resolved = @()
    foreach ($target in $targets) {
        if ($target.PSObject.Properties.Name -contains 'Uri') {
            $hostName = $target.Uri.Host
            $port = $target.Uri.Port
        } else {
            $hostName = $target.Host
            $port = $target.Port
        }
        if ([string]::IsNullOrWhiteSpace($hostName) -or -not (Test-NativeLoopbackHost -HostName $hostName)) {
            throw ("{0} is not local loopback; refusing to migrate, bootstrap, or start." -f $target.Name)
        }
        $resolved += [pscustomobject]@{ Name = $target.Name; Host = $hostName; Port = $port }
    }
    return $resolved
}

function Set-NativeEnvironmentValues {
    param(
        [Parameter(Mandatory = $true)]
        [string] $Path,
        [Parameter(Mandatory = $true)]
        [System.Collections.IDictionary] $Values
    )

    $lines = @(Get-Content -LiteralPath $Path)
    $updated = @(Set-NativeEnvKeyValues -Lines $lines -Values $Values)
    $tempPath = $Path + '.tmp-' + [Guid]::NewGuid().ToString('N')
    $backupPath = $Path + '.bak-' + [Guid]::NewGuid().ToString('N')
    $encoding = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllLines($tempPath, $updated, $encoding)
    try {
        [System.IO.File]::Replace($tempPath, $Path, $backupPath)
        [System.IO.File]::Delete($backupPath)
    } catch {
        [System.IO.File]::Delete($tempPath)
        if ([System.IO.File]::Exists($backupPath)) {
            [System.IO.File]::Delete($backupPath)
        }
        throw
    }
}

function Update-NativeApiEndpointConfiguration {
    param(
        [Parameter(Mandatory = $true)]
        [pscustomobject] $Context
    )

    $apiBaseUrl = Get-NativeApiBaseUrl
    $webBaseUrl = Get-NativeWebBaseUrl
    $managedNames = @('root', 'api', 'web', 'admin', 'space', 'live')

    function ConvertTo-NativeWebOriginList {
        param([string] $Value)

        $origins = @(
            $Value -split ',' |
                ForEach-Object { $_.Trim() } |
                Where-Object { $_ } |
                ForEach-Object {
                    if ($_ -in @('http://localhost:3000', 'http://127.0.0.1:3000')) {
                        $webBaseUrl
                    } else {
                        $_
                    }
                }
        )
        if ($origins -notcontains $webBaseUrl) {
            $origins += $webBaseUrl
        }
        return (@($origins | Select-Object -Unique) -join ',')
    }

    $updated = @()

    foreach ($file in @(Get-NativeEnvironmentFiles -Context $Context | Where-Object {
        $_.Name -in $managedNames
    })) {
        $firstLine = Get-Content -LiteralPath $file.Path -TotalCount 1
        if ($firstLine -ne '# Generated by dev/setup-native.ps1. Existing files are never replaced.') {
            throw ("Refusing to change native endpoints in a non-generated environment file: {0}" -f $file.Path)
        }

        $current = Read-NativeEnvironmentFile -Path $file.Path
        $desired = [ordered]@{}
        switch ($file.Name) {
            'api' {
                $desired['WEB_URL'] = $webBaseUrl
                $desired['APP_BASE_URL'] = $webBaseUrl
            }
            'web' {
                $desired['VITE_API_BASE_URL'] = $webBaseUrl
                $desired['VITE_WEB_BASE_URL'] = $webBaseUrl
                $desired['NATIVE_API_PROXY_TARGET'] = $apiBaseUrl
            }
            'admin' {
                $desired['VITE_API_BASE_URL'] = $webBaseUrl
                $desired['VITE_WEB_BASE_URL'] = $webBaseUrl
            }
            'space' {
                $desired['VITE_API_BASE_URL'] = $webBaseUrl
                $desired['VITE_WEB_BASE_URL'] = $webBaseUrl
            }
            'live' {
                $desired['API_BASE_URL'] = $apiBaseUrl
            }
        }
        foreach ($originKey in @('CORS_ALLOWED_ORIGINS', 'CSRF_TRUSTED_ORIGINS')) {
            if ($current.ContainsKey($originKey)) {
                $desired[$originKey] = ConvertTo-NativeWebOriginList -Value ([string] $current[$originKey])
            }
        }

        $changes = [ordered]@{}
        foreach ($key in $desired.Keys) {
            $expected = [string] $desired[$key]
            if (-not $current.ContainsKey($key) -or [string] $current[$key] -ne $expected) {
                $changes[$key] = $expected
            }
        }
        if ($changes.Count -gt 0) {
            Set-NativeEnvironmentValues -Path $file.Path -Values $changes
            $updated += $file.Path
        }
    }

    return [pscustomobject]@{
        ApiBaseUrl = $apiBaseUrl
        WebBaseUrl = $webBaseUrl
        Updated = $updated
    }
}

function Update-NativeEnvironmentCredentials {
    param(
        [Parameter(Mandatory = $true)]
        [pscustomobject] $Context,
        [Parameter(Mandatory = $true)]
        [string] $PostgresUser,
        [Parameter(Mandatory = $true)]
        [string] $PostgresPassword,
        [Parameter(Mandatory = $true)]
        [string] $PostgresDatabase,
        [Parameter(Mandatory = $true)]
        [string] $RabbitUser,
        [Parameter(Mandatory = $true)]
        [string] $RabbitPassword,
        [Parameter(Mandatory = $true)]
        [string] $RabbitVHost
    )

    $files = Get-NativeEnvironmentFiles -Context $Context
    $apiFile = $files | Where-Object { $_.Name -eq 'api' }
    [void] (Assert-NativeLocalEnvironment -ApiEnvPath $apiFile.Path)
    $values = Read-NativeEnvironmentFile -Path $apiFile.Path
    $databaseUrl = 'postgresql://{0}:{1}@{2}:{3}/{4}' -f `
        [Uri]::EscapeDataString($PostgresUser), `
        [Uri]::EscapeDataString($PostgresPassword), `
        $values['POSTGRES_HOST'], `
        $values['POSTGRES_PORT'], `
        $PostgresDatabase
    $amqpUrl = 'amqp://{0}:{1}@{2}:{3}/{4}' -f `
        [Uri]::EscapeDataString($RabbitUser), `
        [Uri]::EscapeDataString($RabbitPassword), `
        $values['RABBITMQ_HOST'], `
        $values['RABBITMQ_PORT'], `
        [Uri]::EscapeDataString($RabbitVHost)

    $updates = [ordered]@{
        POSTGRES_USER = $PostgresUser
        POSTGRES_PASSWORD = $PostgresPassword
        POSTGRES_DB = $PostgresDatabase
        DATABASE_URL = $databaseUrl
        RABBITMQ_USER = $RabbitUser
        RABBITMQ_PASSWORD = $RabbitPassword
        RABBITMQ_VHOST = $RabbitVHost
        AMQP_URL = $amqpUrl
    }

    $updated = @()
    foreach ($file in @($files | Where-Object { $_.Name -in @('root', 'api') })) {
        $firstLine = Get-Content -LiteralPath $file.Path -TotalCount 1
        if ($firstLine -ne '# Generated by dev/setup-native.ps1. Existing files are never replaced.') {
            throw ("Refusing to change credentials in a non-generated environment file: {0}" -f $file.Path)
        }
        Set-NativeEnvironmentValues -Path $file.Path -Values $updates
        $updated += $file.Path
    }
    return $updated
}

function Test-NativeWebInstanceReadiness {
    param(
        [Parameter(Mandatory = $true)]
        [string] $ApiBaseUrl,
        [string] $Origin = (Get-NativeWebBaseUrl),
        [switch] $RequireSetupDone,
        [scriptblock] $RequestProvider
    )

    if (-not $RequestProvider) {
        $RequestProvider = {
            param([string] $Uri, [hashtable] $Headers)
            Invoke-WebRequest -UseBasicParsing -Uri $Uri -Headers $Headers -TimeoutSec 15 -ErrorAction Stop
        }
    }

    $instanceUri = $ApiBaseUrl.TrimEnd('/') + '/api/instances/'
    try {
        $response = & $RequestProvider $instanceUri @{ Origin = $Origin }
    } catch {
        return [pscustomobject]@{ Ok = $false; Uri = $instanceUri; Reason = 'request-failed' }
    }

    $headers = $response.Headers
    $allowOrigin = [string] ($headers['Access-Control-Allow-Origin'])
    $allowCredentials = [string] ($headers['Access-Control-Allow-Credentials'])
    $contentType = [string] ($headers['Content-Type'])
    if ([int] $response.StatusCode -ne 200) {
        return [pscustomobject]@{ Ok = $false; Uri = $instanceUri; Reason = 'http-status' }
    }
    if ($contentType -notmatch 'application/json') {
        return [pscustomobject]@{ Ok = $false; Uri = $instanceUri; Reason = 'content-type' }
    }
    if ($allowOrigin -ne $Origin -or $allowCredentials -ne 'true') {
        return [pscustomobject]@{ Ok = $false; Uri = $instanceUri; Reason = 'cors' }
    }

    try {
        $payload = $response.Content | ConvertFrom-Json
    } catch {
        return [pscustomobject]@{ Ok = $false; Uri = $instanceUri; Reason = 'malformed-json' }
    }
    if ($null -eq $payload -or $payload -isnot [pscustomobject]) {
        return [pscustomobject]@{ Ok = $false; Uri = $instanceUri; Reason = 'missing-instance-config' }
    }
    $payloadProperties = $payload.PSObject.Properties.Name
    if ($payloadProperties -notcontains 'instance' -or $payloadProperties -notcontains 'config') {
        return [pscustomobject]@{ Ok = $false; Uri = $instanceUri; Reason = 'missing-instance-config' }
    }
    if (
        $null -eq $payload.instance -or
        $payload.instance -isnot [pscustomobject] -or
        $null -eq $payload.config -or
        $payload.config -isnot [pscustomobject]
    ) {
        return [pscustomobject]@{ Ok = $false; Uri = $instanceUri; Reason = 'missing-instance-config' }
    }

    $setupProperty = $payload.instance.PSObject.Properties['is_setup_done']
    $activatedProperty = $payload.instance.PSObject.Properties['is_activated']
    $selfManagedProperty = $payload.config.PSObject.Properties['is_self_managed']
    if (-not $setupProperty -or -not $selfManagedProperty) {
        return [pscustomobject]@{ Ok = $false; Uri = $instanceUri; Reason = 'invalid-boolean-shape' }
    }
    $setupDone = $setupProperty.Value
    $activated = if ($activatedProperty) { $activatedProperty.Value } else { $null }
    $selfManaged = $selfManagedProperty.Value
    if ($setupDone -isnot [bool] -or $selfManaged -isnot [bool]) {
        return [pscustomobject]@{ Ok = $false; Uri = $instanceUri; Reason = 'invalid-boolean-shape' }
    }
    if ($activatedProperty -and $activated -isnot [bool]) {
        return [pscustomobject]@{ Ok = $false; Uri = $instanceUri; Reason = 'invalid-boolean-shape' }
    }
    if ($RequireSetupDone -and -not $setupDone) {
        return [pscustomobject]@{ Ok = $false; Uri = $instanceUri; Reason = 'setup-not-done' }
    }

    return [pscustomobject]@{
        Ok = $true
        Uri = $instanceUri
        Reason = 'ready'
        SetupDone = $setupDone
        Activated = $activated
        SelfManaged = $selfManaged
        Cors = "$Origin + credentials"
    }
}

Export-ModuleMember -Function @(
    'Get-NativeEnvironmentFiles',
    'Read-NativeEnvironmentFile',
    'ConvertFrom-NativeEnvValue',
    'ConvertTo-NativeEnvFileValue',
    'Set-NativeEnvKeyValues',
    'New-NativeEnvironmentConfiguration',
    'Test-NativeEnvironmentConfiguration',
    'Test-NativeLoopbackHost',
    'Assert-NativeLocalEnvironment',
    'Set-NativeEnvironmentValues',
    'Update-NativeApiEndpointConfiguration',
    'Update-NativeEnvironmentCredentials',
    'Test-NativeWebInstanceReadiness'
)
