# =====================================================================
#  闲鱼 Agent 服务框架 —— 一键启动（被 启动.bat 调用）
#
#  重要：本文件必须保存为 **UTF-8 with BOM**。
#  Windows PowerShell 5.1 读没有 BOM 的 UTF-8 文件会按 GBK 解析，
#  中文会变成乱码并触发 "The string is missing the terminator" 语法错误。
# =====================================================================
$ErrorActionPreference = 'Continue'
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}

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

Write-Title '闲鱼 Agent 服务框架'

$guiUrl = 'http://127.0.0.1:8787'

# ---------- 1. 找 Python ----------
$venvPy = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path $venvPy)) {
    Write-Host '[1/3] 首次运行，正在创建虚拟环境并安装依赖（约 1~3 分钟）...' -ForegroundColor Yellow
    $sysPy = $null
    foreach ($cand in @('py', 'python')) {
        try {
            & $cand -3 -c "import sys" 2>$null
            if ($LASTEXITCODE -eq 0) { $sysPy = $cand; break }
        } catch {}
    }
    if (-not $sysPy) {
        Write-Host '[错误] 没有找到 Python，请先安装 Python 3.10+ 并在安装时勾选 Add to PATH' -ForegroundColor Red
        Read-Host '按回车退出'
        exit 1
    }
    & $sysPy -3 -m venv (Join-Path $root '.venv')
    if (-not (Test-Path $venvPy)) {
        Write-Host '[错误] 虚拟环境创建失败' -ForegroundColor Red
        Read-Host '按回车退出'
        exit 1
    }
    & $venvPy -m pip install --upgrade pip -q
    & $venvPy -m pip install -r (Join-Path $root 'requirements.txt')
    Write-Host '[1/3] 依赖安装完成' -ForegroundColor Green
} else {
    Write-Host '[1/3] 虚拟环境已就绪' -ForegroundColor Green
}

# ---------- 2. 检查 Python 依赖是否齐全（requirements 变过也不会漏装） ----------
& $venvPy -c "import fastapi, uvicorn, openai, websockets, loguru" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host '[2/3] 检测到依赖缺失，正在补齐...' -ForegroundColor Yellow
    & $venvPy -m pip install -r (Join-Path $root 'requirements.txt')
} else {
    Write-Host '[2/3] 依赖检查通过' -ForegroundColor Green
}

# ---------- 3. 常驻运行（崩了自动重启） ----------
Write-Host '[3/3] 启动服务（管理后台 + 机器人），按 Ctrl+C 退出' -ForegroundColor Green
Write-Host ''
Write-Host '提示：' -ForegroundColor DarkGray
Write-Host "  · 管理后台：$guiUrl   （稍后自动打开浏览器）" -ForegroundColor DarkGray
Write-Host '  · 第一次用：后台「基础配置」填大模型 Key → 点「扫码登录」→ 顶栏点「启动」' -ForegroundColor DarkGray
Write-Host '  · 电脑不要休眠，否则会掉线' -ForegroundColor DarkGray
Write-Host '  · 在闲鱼聊天里单独发一个句号「。」= 人工接管，再发一次 = 恢复 AI' -ForegroundColor DarkGray
Write-Host ''

# 用独立进程延迟打开浏览器：等后台真的起来了再开，失败也不影响主程序
try {
    $opener = "for(`$i=0; `$i -lt 30; `$i++){ Start-Sleep -Seconds 1; " +
              "try{ if((Invoke-WebRequest '$guiUrl' -TimeoutSec 2 -UseBasicParsing).StatusCode -eq 200){ " +
              "Start-Process '$guiUrl'; break } }catch{} }"
    Start-Process -FilePath 'powershell' -WindowStyle Hidden `
        -ArgumentList @('-NoProfile', '-Command', $opener)
} catch {
    Write-Host '（自动打开浏览器失败，请手动访问上面的地址）' -ForegroundColor DarkGray
}

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
        Write-Host '[错误] 程序反复异常退出，已停止自动重启。请把上面的报错截图给开发者。' -ForegroundColor Red
        break
    }
    Write-Host "[警告] 程序异常退出（第 $restarts 次），15 秒后自动重启..." -ForegroundColor Yellow
    Start-Sleep -Seconds 15
}

Read-Host '按回车关闭窗口'
