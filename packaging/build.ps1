param([string]$Python = 'python', [string]$Output = 'dist')
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
Push-Location -LiteralPath $projectRoot
try {
    $env:PYTHONDONTWRITEBYTECODE = '1'
    & $Python -m PyInstaller --noconfirm --clean --distpath $Output packaging/GameBananaIndex.spec
    if ($LASTEXITCODE -ne 0) { throw 'EXE build failed.' }
} finally { Pop-Location }
