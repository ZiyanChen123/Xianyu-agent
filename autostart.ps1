# =====================================================================
#  开机自启 安装 / 卸载
#    安装:  powershell -ExecutionPolicy Bypass -File autostart.ps1 -Action Install
#    卸载:  powershell -ExecutionPolicy Bypass -File autostart.ps1 -Action Uninstall
# =====================================================================
param(
    [ValidateSet('Install', 'Uninstall', 'Status')]
    [string]$Action = 'Install'
)

$ErrorActionPreference = 'Stop'
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}

$root = $PSScriptRoot
$taskName = 'XianyuAIShop'
$bat = Join-Path $root '启动.bat'

function Show-Status {
    $t = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    if ($t) {
        Write-Host "✅ 开机自启已安装：$taskName" -ForegroundColor Green
        Write-Host "   状态: $($t.State)   触发器: $($t.Triggers.CimClass.CimClassName -join ',')" -ForegroundColor Gray
    } else {
        Write-Host '当前没有安装开机自启。' -ForegroundColor Yellow
    }
}

switch ($Action) {
    'Uninstall' {
        if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) {
            Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
            Write-Host "✅ 已取消开机自启：$taskName" -ForegroundColor Green
        } else {
            Write-Host '本来就没有安装开机自启。' -ForegroundColor Yellow
        }
        Show-Status
    }

    'Status' { Show-Status }

    'Install' {
        if (-not (Test-Path $bat)) {
            Write-Host "❌ 找不到 $bat" -ForegroundColor Red
            exit 1
        }

        # 已有同名任务先清掉，保证可重复执行
        if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) {
            Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
        }

        $action = New-ScheduledTaskAction -Execute $bat -WorkingDirectory $root
        $trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
        $settings = New-ScheduledTaskSettingsSet `
            -AllowStartIfOnBatteries `
            -DontStopIfGoingOnBatteries `
            -StartWhenAvailable `
            -RestartCount 3 `
            -RestartInterval (New-TimeSpan -Minutes 5) `
            -ExecutionTimeLimit (New-TimeSpan -Days 0)

        Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
            -Settings $settings -Description '闲鱼 AI 生图店铺自动值守（开机登录后自动启动）' | Out-Null

        Write-Host "✅ 已安装开机自启：$taskName" -ForegroundColor Green
        Write-Host '   下次登录 Windows 后会自动启动 启动.bat' -ForegroundColor Gray
        Write-Host ''
        Write-Host '是否现在顺便关闭【自动睡眠】，避免挂机掉线？' -ForegroundColor Yellow
        $ans = Read-Host '   输入 y 关闭睡眠（推荐），其它键跳过'
        if ($ans -eq 'y' -or $ans -eq 'Y') {
            powercfg /change standby-timeout-ac 0
            powercfg /change hibernate-timeout-ac 0
            powercfg /change monitor-timeout-ac 15
            Write-Host '✅ 已设置：接电源时永不睡眠（屏幕 15 分钟后关闭，不影响运行）' -ForegroundColor Green
        }
        Show-Status
    }
}
