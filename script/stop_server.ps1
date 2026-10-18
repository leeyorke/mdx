# ─────────────────────────────────────────────────────────────────────
#  停止正在运行的 MDX 服务（按 4080 端口找进程并结束）
#
#  用法： pwsh script/stop_server.ps1
#  说明： 只结束监听 4080 端口的进程，不影响其他 python 程序
# ─────────────────────────────────────────────────────────────────────
$ErrorActionPreference = "Stop"

$port = 4080
$conns = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
if (-not $conns) {
    Write-Output "没有在监听 $port 端口的进程"
    return
}

$pids = $conns | Select-Object -ExpandProperty OwningProcess -Unique
foreach ($procId in $pids) {
    try {
        $proc = Get-Process -Id $procId -ErrorAction Stop
        Stop-Process -Id $procId -Force
        Write-Output "已停止: $($proc.ProcessName) (PID $procId)"
    }
    catch {
        Write-Output "停止 PID $procId 失败: $($_.Exception.Message)"
    }
}
