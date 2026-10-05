# 公共变量与函数（被其它脚本 dot-source，不单独运行）
$ErrorActionPreference = 'Stop'

$script:RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$script:PythonExe = if ($env:XYWB_PYTHON) { $env:XYWB_PYTHON } else { 'C:\Python310\python.exe' }

function Write-Step([string]$Message) {
    Write-Host "==> $Message" -ForegroundColor Cyan
}

function Write-Ok([string]$Message) {
    Write-Host "[ok] $Message" -ForegroundColor Green
}

function Assert-Python {
    if (-not (Test-Path $script:PythonExe)) {
        throw "找不到 Python: $script:PythonExe（可用环境变量 XYWB_PYTHON 覆盖）"
    }
}
