# =====================================================================
#  自检（被 自检.bat 调用）
# =====================================================================
$ErrorActionPreference = 'Continue'
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}

$root = $PSScriptRoot
Set-Location $root
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'

$py = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path $py)) { $py = 'python' }

Write-Host ''
Write-Host '=' * 66 -ForegroundColor DarkCyan
Write-Host '  自检：会真实调用一次大模型和一次生图（约 0.04 元）' -ForegroundColor Cyan
Write-Host '=' * 66 -ForegroundColor DarkCyan
Write-Host ''

& $py (Join-Path $root 'selftest.py')
