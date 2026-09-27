# =====================================================================
#  Cookie 体检（被 体检.bat 调用）
#  出错时先跑这个，把输出整段发出来，就能定位问题
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
Write-Host '  Cookie 体检' -ForegroundColor Cyan
Write-Host '=' * 66 -ForegroundColor DarkCyan

& $py (Join-Path $root 'cookie_login.py') --check

Write-Host ''
Write-Host '最新日志末尾（方便排查）:' -ForegroundColor DarkGray
$log = Get-ChildItem (Join-Path $root 'logs') -Filter 'shop_*.log' -ErrorAction SilentlyContinue |
       Sort-Object LastWriteTime -Descending | Select-Object -First 1
if ($log) { Get-Content $log.FullName -Tail 25 -Encoding utf8 }
else { Write-Host '（还没有日志文件）' -ForegroundColor DarkGray }
