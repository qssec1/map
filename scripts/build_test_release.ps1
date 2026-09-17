param(
    [string]$Python = "python"
)

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$testRoot = "C:\Users\admin\Desktop\仿真新版本测试"
$testApp = Join-Path $testRoot "MapFanSim_新版测试"
$zipPath = Join-Path $testRoot "MapFanSim-windows-x64.zip"

Set-Location $root
New-Item -ItemType Directory -Force -Path $testRoot | Out-Null

& (Join-Path $PSScriptRoot "build_collector.ps1") -Python $Python

& $Python -m PyInstaller `
    --noconfirm `
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

$distDir = Join-Path $root "dist\MapFanSim"
if (-not (Test-Path $distDir)) {
    throw "PyInstaller 输出不存在：$distDir"
}

if (Test-Path $testApp) {
    Rename-Item -LiteralPath $testApp -NewName ("MapFanSim_新版测试_old_" + (Get-Date -Format "yyyyMMdd_HHmmss"))
}
New-Item -ItemType Directory -Force -Path $testApp | Out-Null
Copy-Item -Recurse -Force -Path (Join-Path $distDir "*") -Destination $testApp

foreach ($dir in @("rules", "input_maps")) {
    $src = Join-Path $root $dir
    if (Test-Path $src) {
        Copy-Item -Recurse -Force -LiteralPath $src -Destination $testApp
    }
}

$dataSrc = Join-Path $root "data"
$dataDst = Join-Path $testApp "data"
if (Test-Path $dataSrc) {
    New-Item -ItemType Directory -Force -Path $dataDst | Out-Null
    Get-ChildItem -LiteralPath $dataSrc -File | Where-Object { $_.Name -ne "config.json" -and $_.Name -ne "config.local.json" } | ForEach-Object {
        Copy-Item -Force -LiteralPath $_.FullName -Destination (Join-Path $dataDst $_.Name)
    }
}

$toolsSrc = Join-Path $root "tools"
$toolsDst = Join-Path $testApp "tools"
if (Test-Path $toolsSrc) {
    New-Item -ItemType Directory -Force -Path $toolsDst | Out-Null
    Get-ChildItem -LiteralPath $toolsSrc | ForEach-Object {
        Copy-Item -Recurse -Force -LiteralPath $_.FullName -Destination $toolsDst
    }
}

foreach ($dir in @("output_maps", "download", "update", "backup", "reports", "logs", "tools")) {
    New-Item -ItemType Directory -Force -Path (Join-Path $testApp $dir) | Out-Null
}

if (Test-Path $zipPath) {
    Rename-Item -LiteralPath $zipPath -NewName ("MapFanSim-windows-x64_old_" + (Get-Date -Format "yyyyMMdd_HHmmss") + ".zip")
}
Compress-Archive -Path (Join-Path $testApp "*") -DestinationPath $zipPath -Force

Write-Host "Test release created: $testApp"
Write-Host "Test zip created: $zipPath"
Write-Host "Normal app directory was not modified: C:\Users\admin\Desktop\htaexe\MapFanSim"
