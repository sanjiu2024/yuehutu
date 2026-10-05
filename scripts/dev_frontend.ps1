# 开发模式跑前端 Vite dev server（默认 5173，代理 /api 到后端 8080）
#   pwsh scripts\dev_frontend.ps1
. (Join-Path $PSScriptRoot '_common.ps1')

$frontend = Join-Path $script:RepoRoot 'frontend'
if (-not (Test-Path (Join-Path $frontend 'node_modules'))) {
    Write-Step 'frontend/node_modules 不存在，先 npm install'
    Push-Location $frontend
    try { npm install } finally { Pop-Location }
}

Write-Step 'Vite dev server 启动中（Ctrl+C 退出）'
Push-Location $frontend
try { npm run dev } finally { Pop-Location }
