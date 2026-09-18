param(
    [string]$Python = "python",
    [string]$OutputDirectory = ""
)

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

& (Join-Path $PSScriptRoot "build_collector.ps1") -Python $Python

$releaseRoot = Join-Path $root "release"
$buildStamp = Get-Date -Format "yyyyMMdd_HHmmss_fff"
$releaseDir = if ($OutputDirectory) { [IO.Path]::GetFullPath($OutputDirectory) } else { Join-Path $releaseRoot "MapFanSim_$buildStamp" }
if (Test-Path -LiteralPath $releaseDir) { throw "Output directory already exists: $releaseDir" }
$buildDir = Join-Path $root "build\release_$buildStamp"
$distDir = Join-Path $root "dist\release_$buildStamp"

& $Python -m PyInstaller `
    --noconfirm `
    --clean `
    --windowed `
    --name MapFanSim `
    --workpath $buildDir `
    --distpath $distDir `
    --hidden-import paramiko `
    --hidden-import bcrypt `
    --hidden-import cryptography `
    --hidden-import openpyxl `
    --hidden-import xlrd `
    src\MapFanSim.py
if ($LASTEXITCODE -ne 0) { throw "Main application build failed" }

New-Item -ItemType Directory -Force -Path $releaseRoot | Out-Null
Copy-Item -Recurse -LiteralPath (Join-Path $distDir "MapFanSim") -Destination $releaseDir

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
