# 开发模式跑后端（前端未构建时后端给占位页）
#   pwsh scripts\dev_backend.ps1 [-Port 8080]
param(
    [int]$Port = 8080
)

. (Join-Path $PSScriptRoot '_common.ps1')
Assert-Python

Write-Step "后端: http://127.0.0.1:$Port/  (Ctrl+C 退出)"
$env:PYTHONPATH = $script:RepoRoot
$env:XYWB_PORT = "$Port"
& $script:PythonExe -m backend.app.main --host 127.0.0.1 --port $Port
