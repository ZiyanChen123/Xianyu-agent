# =====================================================================
#  闲鱼 AI 店铺 —— 一键启动（被 启动.bat 调用）
# =====================================================================
$ErrorActionPreference = 'Continue'
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}
try { $OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}

$root = $PSScriptRoot
Set-Location $root
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'

function Write-Title($text) {
    Write-Host ''
    Write-Host ('=' * 66) -ForegroundColor DarkCyan
    Write-Host "  $text" -ForegroundColor Cyan
    Write-Host ('=' * 66) -ForegroundColor DarkCyan
}

Write-Title '闲鱼 AI 生图 / AI 写小说 店铺 —— 自动值守'

# ---------- 1. 找 Python ----------
$venvPy = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path $venvPy)) {
    Write-Host '[1/3] 首次运行，正在创建虚拟环境并安装依赖（约 1~3 分钟）...' -ForegroundColor Yellow
    $sysPy = $null
    foreach ($cand in @('py', 'python')) {
        try {
            $v = & $cand -3 -c "import sys;print(sys.version_info[:2])" 2>$null
            if ($LASTEXITCODE -eq 0) { $sysPy = $cand; break }
        } catch {}
    }
    if (-not $sysPy) {
        Write-Host '❌ 没有找到 Python，请先安装 Python 3.10+ 并勾选 Add to PATH' -ForegroundColor Red
        Read-Host '按回车退出'
        exit 1
    }
    & $sysPy -3 -m venv (Join-Path $root '.venv')
    if (-not (Test-Path $venvPy)) {
        Write-Host '❌ 虚拟环境创建失败' -ForegroundColor Red
        Read-Host '按回车退出'
        exit 1
    }
    & $venvPy -m pip install --upgrade pip -q
    & $venvPy -m pip install -r (Join-Path $root 'requirements.txt')
    Write-Host '✅ 依赖安装完成' -ForegroundColor Green
} else {
    Write-Host '[1/3] 虚拟环境已就绪' -ForegroundColor Green
}

# ---------- 2. 检查 .env ----------
$envFile = Join-Path $root '.env'
if (-not (Test-Path $envFile)) {
    Copy-Item (Join-Path $root '.env.example') $envFile
    Write-Host '[2/3] 已根据模板生成 .env，请在 .env 里填好密钥后重新运行' -ForegroundColor Yellow
    notepad $envFile
    Read-Host '按回车退出'
    exit 1
}
Write-Host '[2/3] 配置文件已就绪' -ForegroundColor Green

# ---------- 3. 常驻运行（崩了自动重启） ----------
Write-Host '[3/3] 启动服务（管理后台 + 机器人），按 Ctrl+C 退出' -ForegroundColor Green
Write-Host ''
Write-Host '提示：' -ForegroundColor DarkGray
Write-Host '  · 管理后台地址：http://127.0.0.1:8787  （稍后会自动打开浏览器）' -ForegroundColor DarkGray
Write-Host '  · 第一次用：在后台「基础配置」填大模型 Key，点「扫码登录」，再点顶栏「启动」' -ForegroundColor DarkGray
Write-Host '  · 电脑不要休眠，否则会掉线' -ForegroundColor DarkGray
Write-Host '  · 在闲鱼聊天里单独发一个句号「。」= 人工接管，再发一次 = 恢复 AI' -ForegroundColor DarkGray
Write-Host ''

# 等后台起来后自动打开浏览器（失败也不影响主程序）
Start-Job -ScriptBlock {
    param($url)
    for ($i = 0; $i -lt 30; $i++) {
        Start-Sleep -Seconds 1
        try {
            $r = Invoke-WebRequest -Uri $url -TimeoutSec 2 -UseBasicParsing
            if ($r.StatusCode -eq 200) { Start-Process $url; break }
        } catch { }
    }
} -ArgumentList 'http://127.0.0.1:8787' | Out-Null

$restarts = 0
while ($true) {
    & $venvPy (Join-Path $root 'main.py')
    $code = $LASTEXITCODE
    if ($code -eq 0) {
        Write-Host '程序正常退出。' -ForegroundColor Yellow
        break
    }
    $restarts++
    if ($restarts -ge 20) {
        Write-Host '❌ 程序反复异常退出，已停止自动重启。请把上面的报错截图给开发者。' -ForegroundColor Red
        break
    }
    Write-Host "⚠️ 程序异常退出（第 $restarts 次），15 秒后自动重启..." -ForegroundColor Yellow
    Start-Sleep -Seconds 15
}

Read-Host '按回车关闭窗口'
