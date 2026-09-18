param(
    [string]$Python = "python"
)

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$collector = Join-Path $root "collector"
$toolsDir = Join-Path $root "tools\风机文件拷取工具"

Push-Location $collector
try {
    & $Python -m PyInstaller `
        --noconfirm `
        --clean `
        --onefile `
        --windowed `
        --name WindFileCollector `
        --collect-all paramiko `
        app.py
    if ($LASTEXITCODE -ne 0) { throw "Collector build failed" }
} finally {
    Pop-Location
}

New-Item -ItemType Directory -Force -Path $toolsDir | Out-Null
Copy-Item -Force -LiteralPath (Join-Path $collector "dist\WindFileCollector.exe") -Destination (Join-Path $toolsDir "风机文件拷取工具.exe")
Copy-Item -Force -LiteralPath (Join-Path $collector "使用说明.txt") -Destination (Join-Path $toolsDir "风机文件拷取工具-使用说明.txt")

Write-Host "Collector package created: $toolsDir"
