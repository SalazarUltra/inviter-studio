$ErrorActionPreference = "Stop"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$python = Join-Path $root ".venv\Scripts\python.exe"
$ruff = Join-Path $root ".audit\tooling\Scripts\ruff.exe"

if (-not (Test-Path -LiteralPath $python)) {
    throw "Python virtual environment not found: .venv"
}
if (-not (Test-Path -LiteralPath $ruff)) {
    throw "Ruff not found in .audit/tooling"
}

$oldSslKeyLog = $env:SSLKEYLOGFILE
$oldTemp = $env:TEMP
$oldTmp = $env:TMP
$env:SSLKEYLOGFILE = ""
$testTemp = Join-Path $root ".audit\test-temp"
New-Item -ItemType Directory -Path $testTemp -Force | Out-Null
$env:TEMP = $testTemp
$env:TMP = $testTemp
try {
    Push-Location $root
    & $ruff check core ui tests tools
    if ($LASTEXITCODE -ne 0) { throw "Ruff checks failed." }
    & $ruff format --check core ui tests tools
    if ($LASTEXITCODE -ne 0) { throw "Python formatting check failed." }
    & $python -m compileall -q core ui tests tools
    if ($LASTEXITCODE -ne 0) { throw "Python syntax check failed." }
    & $python -m unittest discover -s tests -v
    if ($LASTEXITCODE -ne 0) { throw "Unit tests failed." }
    & $python -m pip check
    if ($LASTEXITCODE -ne 0) { throw "Installed dependency check failed." }
} finally {
    Pop-Location
    $env:SSLKEYLOGFILE = $oldSslKeyLog
    $env:TEMP = $oldTemp
    $env:TMP = $oldTmp
}

Write-Host "All project checks passed."
