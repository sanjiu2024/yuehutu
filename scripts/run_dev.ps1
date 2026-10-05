# 一键开发：后端(8080) + 前端 dev(5173) + 桌面窗口
#   pwsh scripts\run_dev.ps1 [-Port 8080]
# 说明：桌面窗口自己会起后端线程，所以这里默认只起前端 dev server 再开窗口；
#       加 -WithBackend 时另起一个独立后端进程（给浏览器里调 /api 用）。
param(
    [int]$Port = 8080,
    [switch]$WithBackend
)

. (Join-Path $PSScriptRoot '_common.ps1')
Assert-Python

$procs = @()

if ($WithBackend) {
    Write-Step "启动后端 :$Port"
    $procs += Start-Process -PassThru -FilePath $script:PythonExe `
        -ArgumentList '-m', 'backend.app.main', '--host', '127.0.0.1', '--port', "$Port" `
        -WorkingDirectory $script:RepoRoot `
        -Environment @{ PYTHONPATH = $script:RepoRoot; XYWB_PORT = "$Port" }
}

Write-Step '启动前端 dev server :5173'
$procs += Start-Process -PassThru -FilePath 'npm' -ArgumentList 'run', 'dev' `
    -WorkingDirectory (Join-Path $script:RepoRoot 'frontend')

Start-Sleep -Seconds 2

try {
    Write-Step '启动桌面窗口（Ctrl+C 或托盘「退出」结束）'
    $env:PYTHONPATH = $script:RepoRoot
    & $script:PythonExe (Join-Path $script:RepoRoot 'desktop\main.py')
} finally {
    foreach ($p in $procs) {
        if ($p -and -not $p.HasExited) {
            Write-Step "结束进程 $($p.Id) $($p.ProcessName)"
            Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue
        }
    }
}
