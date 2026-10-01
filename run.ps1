$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $projectRoot

$pythonCandidates = @()
if ($env:XUNLONG_PYTHON) {
    $pythonCandidates += $env:XUNLONG_PYTHON
}
$systemPython = Join-Path $env:LOCALAPPDATA 'Programs\Python\Python311\python.exe'
if (Test-Path -LiteralPath $systemPython) {
    $pythonCandidates += $systemPython
}
$pathPython = Get-Command python -ErrorAction SilentlyContinue
if ($pathPython) {
    $pythonCandidates += $pathPython.Source
}
$pythonPath = $null
foreach ($candidate in ($pythonCandidates | Select-Object -Unique)) {
    & $candidate -c "import fastapi, uvicorn, requests, pandas, mootdx" 2>$null
    if ($LASTEXITCODE -eq 0) {
        $pythonPath = $candidate
        break
    }
}
if (-not $pythonPath) {
    throw 'Python 3.11 with the required dependencies was not found.'
}

$dataDir = Join-Path $projectRoot 'data'
New-Item -ItemType Directory -Path $dataDir -Force | Out-Null
$stdoutLog = Join-Path $dataDir 'server.current.out.log'
$stderrLog = Join-Path $dataDir 'server.current.err.log'
$serverProcess = Start-Process -FilePath $pythonPath `
    -ArgumentList @('server.py') `
    -WorkingDirectory $projectRoot `
    -WindowStyle Hidden `
    -RedirectStandardOutput $stdoutLog `
    -RedirectStandardError $stderrLog `
    -PassThru `
    -Wait
exit $serverProcess.ExitCode
