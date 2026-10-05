# 一键开发：前端 dev(5173) + 桌面窗口（可选独立后端 8080）
#   pwsh scripts\run_dev.ps1 [-Port 8080] [-WithBackend]
# 说明：桌面窗口自己会起后端线程，所以这里默认只起前端 dev server 再开窗口；
#       加 -WithBackend 时另起一个独立后端进程（给浏览器里直接调 /api 用）。
# 注意：本脚本兼容 Windows PowerShell 5.1（Start-Process 没有 -Environment）。
param(
    [int]$Port = 8080,
    [switch]$WithBackend
)

. (Join-Path $PSScriptRoot '_common.ps1')
Assert-Python

$procs = @()

# 环境变量在子进程里是继承的，所以在当前进程设好再 Start-Process
$env:PYTHONPATH = $script:RepoRoot
$env:XYWB_PORT = "$Port"

try {
    if ($WithBackend) {
        Write-Step "启动后端 :$Port"
        $procs += Start-Process -PassThru -FilePath $script:PythonExe `
            -ArgumentList '-m', 'backend.app.main', '--host', '127.0.0.1', '--port', "$Port" `
            -WorkingDirectory $script:RepoRoot
    }

    Write-Step '启动前端 dev server :5173'
    $procs += Start-Process -PassThru -FilePath 'npm.cmd' -ArgumentList 'run', 'dev' `
        -WorkingDirectory (Join-Path $script:RepoRoot 'frontend')

    Start-Sleep -Seconds 2

    Write-Step '启动桌面窗口（Ctrl+C 或托盘「退出」结束）'
    & $script:PythonExe (Join-Path $script:RepoRoot 'desktop\main.py')
} finally {
    foreach ($p in $procs) {
        if ($p -and -not $p.HasExited) {
            Write-Step "结束进程 $($p.Id) $($p.ProcessName)"
            Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue
        }
    }
}
