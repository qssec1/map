param(
    [string]$Python = "python"
)

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

& (Join-Path $PSScriptRoot "build_collector.ps1") -Python $Python

$releaseRoot = Join-Path $root "release"
$releaseDir = Join-Path $releaseRoot "MapFanSim"

Get-Process MapFanSim -ErrorAction SilentlyContinue | Stop-Process -Force

foreach ($path in @("build", "dist", "release", "发布成品", "鍙戝竷鎴愬搧")) {
    $target = Join-Path $root $path
    if (Test-Path $target) {
        Remove-Item -Recurse -Force -LiteralPath $target
    }
}

& $Python -m PyInstaller `
    --noconfirm `
    --clean `
    --windowed `
    --name MapFanSim `
    --add-data "data;data" `
    --add-data "input_maps;input_maps" `
    --add-data "output_maps;output_maps" `
    --add-data "download;download" `
    --add-data "update;update" `
    --add-data "backup;backup" `
    --add-data "reports;reports" `
    --add-data "logs;logs" `
    --add-data "rules;rules" `
    --hidden-import paramiko `
    --hidden-import bcrypt `
    --hidden-import cryptography `
    --hidden-import openpyxl `
    --hidden-import xlrd `
    src\MapFanSim.py

New-Item -ItemType Directory -Force -Path $releaseRoot | Out-Null
Copy-Item -Recurse -Force -LiteralPath (Join-Path $root "dist\MapFanSim") -Destination $releaseDir

foreach ($dir in @("rules", "input_maps")) {
    $src = Join-Path $root $dir
    $dst = Join-Path $releaseDir $dir
    if (Test-Path $src) {
        Copy-Item -Recurse -Force -LiteralPath $src -Destination $dst
    }
}

$dataSrc = Join-Path $root "data"
$dataDst = Join-Path $releaseDir "data"
if (Test-Path $dataSrc) {
    New-Item -ItemType Directory -Force -Path $dataDst | Out-Null
    Get-ChildItem -LiteralPath $dataSrc -File | Where-Object { $_.Name -ne "config.json" -and $_.Name -ne "config.local.json" } | ForEach-Object {
        Copy-Item -Force -LiteralPath $_.FullName -Destination (Join-Path $dataDst $_.Name)
    }
}

$toolsSrc = Join-Path $root "tools"
$toolsDst = Join-Path $releaseDir "tools"
if (Test-Path $toolsSrc) {
    New-Item -ItemType Directory -Force -Path $toolsDst | Out-Null
    Get-ChildItem -LiteralPath $toolsSrc | ForEach-Object {
        Copy-Item -Recurse -Force -LiteralPath $_.FullName -Destination $toolsDst
    }
}

foreach ($dir in @("output_maps", "download", "update", "backup", "reports", "logs", "tools")) {
    New-Item -ItemType Directory -Force -Path (Join-Path $releaseDir $dir) | Out-Null
}

Write-Host "Release created: $releaseDir"
