$ErrorActionPreference = 'Stop'

$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$url = 'http://127.0.0.1:8765/'
$healthUrl = $url + 'api/health'
$expectedDatabase = [System.IO.Path]::GetFullPath((Join-Path $projectRoot 'data\xunlong.db'))
$errorLog = Join-Path $projectRoot 'data\launcher.error.log'

function Get-WorkbenchHealth {
    try {
        return Invoke-RestMethod -Uri $healthUrl -TimeoutSec 2
    }
    catch {
        return $null
    }
}

function Test-ExpectedWorkbench([object]$health) {
    if ($null -eq $health) {
        return $false
    }

    try {
        $actualDatabase = [System.IO.Path]::GetFullPath([string]$health.database)
    }
    catch {
        return $false
    }

    # Provider degradation must not prevent the local UI from opening.
    return [string]$health.version -eq '1.0.0' -and $actualDatabase -ieq $expectedDatabase
}

function Test-PortInUse {
    return $null -ne (Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1)
}

function Open-Workbench {
    $edgeCandidates = @(
        'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe',
        'C:\Program Files\Microsoft\Edge\Application\msedge.exe'
    )
    $edgeExe = $edgeCandidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1

    if ($edgeExe) {
        # Use Edge app mode so the workbench gets its own visible desktop
        # window instead of being swallowed by an existing tab/session.
        Start-Process -FilePath $edgeExe -ArgumentList @('--app=' + $url)
    }
    else {
        Start-Process $url
    }
}

try {
    Write-Host '[1/3] Checking Xunlong Workbench...'
    $health = Get-WorkbenchHealth

    if (-not (Test-ExpectedWorkbench $health)) {
        if (Test-PortInUse) {
            # A service may still be starting. Give it a short grace period.
            for ($attempt = 0; $attempt -lt 10; $attempt++) {
                Start-Sleep -Milliseconds 500
                $health = Get-WorkbenchHealth
                if (Test-ExpectedWorkbench $health) {
                    break
                }
            }
        }

        if (-not (Test-ExpectedWorkbench $health)) {
            if (Test-PortInUse) {
                throw 'Port 8765 is occupied by a different or unresponsive service.'
            }

            Write-Host '[2/3] Starting Xunlong Workbench...'
            $runScript = Join-Path $projectRoot 'run.ps1'
            $runArguments = '-NoProfile -ExecutionPolicy Bypass -File "{0}"' -f $runScript
            Start-Process -FilePath 'powershell.exe' `
                -ArgumentList $runArguments `
                -WorkingDirectory $projectRoot `
                -WindowStyle Hidden | Out-Null

            $health = $null
            for ($attempt = 0; $attempt -lt 60; $attempt++) {
                Start-Sleep -Milliseconds 500
                $health = Get-WorkbenchHealth
                if (Test-ExpectedWorkbench $health) {
                    break
                }
            }

            if (-not (Test-ExpectedWorkbench $health)) {
                throw 'The service did not become ready within 30 seconds. Check data\server.current.err.log.'
            }
        }
    }

    Write-Host '[3/3] Opening Xunlong Workbench in Microsoft Edge...'
    Open-Workbench
    Write-Host 'Ready.'
    exit 0
}
catch {
    $message = $_.Exception.Message
    $timestamp = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
    New-Item -ItemType Directory -Path (Split-Path -Parent $errorLog) -Force | Out-Null
    "[$timestamp] $message" | Set-Content -LiteralPath $errorLog -Encoding UTF8
    Write-Host ''
    Write-Host "ERROR: $message" -ForegroundColor Red
    Write-Host "Details: $errorLog"
    exit 1
}
