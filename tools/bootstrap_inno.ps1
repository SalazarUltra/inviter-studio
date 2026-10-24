# Verified compiler in portable mode; no system install or file associations.
$ErrorActionPreference = 'Stop'
$taskRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$taskCompiler = Join-Path $taskRoot '.audit\tooling\inno'
$taskBootstrap = Join-Path $taskRoot '.audit\tooling\inno-bootstrap'
New-Item -ItemType Directory -Force -Path $taskBootstrap | Out-Null
$taskDownload = Join-Path $taskBootstrap 'innosetup-6.7.3.exe'
$taskExpectedHash = '9C73C3BAE7ED48D44112A0F48E66742C00090BDB5BEF71D9D3C056C66E97B732'
if (-not (Test-Path -LiteralPath $taskDownload)) {
    Invoke-WebRequest -Uri 'https://github.com/jrsoftware/issrc/releases/download/is-6_7_3/innosetup-6.7.3.exe' -OutFile $taskDownload
}
if ((Get-FileHash -Algorithm SHA256 -LiteralPath $taskDownload).Hash -ne $taskExpectedHash) { throw 'Inno Setup SHA-256 mismatch.' }
$taskSignature = Get-AuthenticodeSignature -LiteralPath $taskDownload
if ($taskSignature.Status -ne 'Valid' -or $taskSignature.SignerCertificate.Subject -notlike '*Pyrsys B.V.*') { throw 'Inno Setup signature is not valid.' }
$taskProcess = Start-Process -FilePath $taskDownload -ArgumentList @('/VERYSILENT','/SUPPRESSMSGBOXES','/NORESTART','/SP-','/CURRENTUSER','/PORTABLE=1',('/DIR="'+$taskCompiler+'"')) -WindowStyle Hidden -PassThru
if (-not $taskProcess.WaitForExit(60000)) { Stop-Process -Id $taskProcess.Id; throw 'Compiler extraction timed out.' }
if ($taskProcess.ExitCode -ne 0 -or -not (Test-Path -LiteralPath (Join-Path $taskCompiler 'ISCC.exe'))) { throw 'Compiler extraction failed.' }
Write-Output ('Compiler ready: ' + (Join-Path $taskCompiler 'ISCC.exe'))
