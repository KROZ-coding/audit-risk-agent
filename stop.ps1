<#
.SYNOPSIS
    停止审计风险识别系统后端服务（Windows PowerShell）

.DESCRIPTION
    按端口查找并终止占用该端口的进程，默认端口 5000。
    找不到端口占用时，回退按命令行匹配 src\main.py 的 python 进程兜底清理。

.PARAMETER Port
    要停止的服务端口，默认 5000。

.EXAMPLE
    .\stop.ps1
    # 停止端口 5000 上的服务

.EXAMPLE
    .\stop.ps1 -Port 8080
    # 停止端口 8080 上的服务
#>
param(
    [int]$Port = 5000
)

Write-Host ""
Write-Host "▶ 正在停止端口 $Port 上的后端服务..." -ForegroundColor Cyan

$stopped = $false

# ── 方式一：按端口定位监听进程并终止 ──
try {
    $conns = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    if ($conns) {
        $pids = $conns.OwningProcess | Select-Object -Unique
        foreach ($procId in $pids) {
            try {
                $p = Get-Process -Id $procId -ErrorAction SilentlyContinue
                $name = if ($p) { $p.ProcessName } else { "unknown" }
                Stop-Process -Id $procId -Force -ErrorAction Stop
                Write-Host "  ✔ 已终止进程 PID $procId ($name)" -ForegroundColor Green
                $stopped = $true
            } catch {
                Write-Host "  ✘ 无法终止 PID $procId : $_" -ForegroundColor Red
            }
        }
    }
} catch {
    Write-Host "  ⚠ 按端口查询失败: $_" -ForegroundColor Yellow
}

# ── 方式二（兜底）：按命令行匹配 main.py 的 python 进程 ──
if (-not $stopped) {
    try {
        $procs = Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
            Where-Object { $_.CommandLine -like '*main.py*' -or $_.CommandLine -like '*uvicorn*' }
        foreach ($proc in $procs) {
            try {
                Stop-Process -Id $proc.ProcessId -Force -ErrorAction Stop
                Write-Host "  ✔ 已终止 python 进程 PID $($proc.ProcessId)" -ForegroundColor Green
                $stopped = $true
            } catch {
                Write-Host "  ✘ 无法终止 PID $($proc.ProcessId): $_" -ForegroundColor Red
            }
        }
    } catch {
        # Win32_Process 在受限环境可能拒绝访问，忽略即可
    }
}

Write-Host ""
if ($stopped) {
    Start-Sleep -Milliseconds 400
    $still = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    if ($still) {
        Write-Host "⚠ 端口 $Port 仍被占用，请重试或手动检查。" -ForegroundColor Yellow
    } else {
        Write-Host "✔ 服务已停止，端口 $Port 已释放。" -ForegroundColor Green
    }
} else {
    Write-Host "ℹ 未发现端口 $Port 上运行的服务（可能已经停止）。" -ForegroundColor Gray
}
Write-Host ""
