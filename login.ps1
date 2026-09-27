# =====================================================================
#  扫码登录 / 换 Cookie（被 扫码登录.bat 调用）
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
Write-Host '  闲鱼扫码登录' -ForegroundColor Cyan
Write-Host '=' * 66 -ForegroundColor DarkCyan
Write-Host ''
Write-Host '马上会弹出二维码图片（data\login_qrcode.png），' -ForegroundColor Yellow
Write-Host '打开手机闲鱼 App → 我的 → 右上角扫一扫 → 扫码并确认。' -ForegroundColor Yellow
Write-Host ''

& $py (Join-Path $root 'cookie_login.py')

Write-Host ''
Write-Host '登录流程结束。如果想确认 Cookie 是否有效，可以再运行一次本脚本并选择检查。' -ForegroundColor Gray
