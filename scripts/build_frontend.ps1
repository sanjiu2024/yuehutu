# 构建前端：frontend/dist（后端会自动静态托管它）
#   pwsh scripts\build_frontend.ps1 [-SkipInstall]
param(
    [switch]$SkipInstall
)

. (Join-Path $PSScriptRoot '_common.ps1')

$frontend = Join-Path $script:RepoRoot 'frontend'
$dist = Join-Path $frontend 'dist'

if (-not (Test-Path (Join-Path $frontend 'package.json'))) {
    throw "找不到 $frontend\package.json"
}

Push-Location $frontend
try {
    if (-not $SkipInstall -or -not (Test-Path (Join-Path $frontend 'node_modules'))) {
        Write-Step 'npm install'
        npm install
        if ($LASTEXITCODE -ne 0) { throw "npm install 失败（exit $LASTEXITCODE）" }
    }

    Write-Step 'npm run build'
    npm run build
    if ($LASTEXITCODE -ne 0) { throw "npm run build 失败（exit $LASTEXITCODE）" }
} finally {
    Pop-Location
}

$index = Join-Path $dist 'index.html'
if (-not (Test-Path $index)) {
    throw "构建结束但没找到 $index"
}
Write-Ok "前端产物: $index"
Get-ChildItem $dist | Select-Object Name, Length | Format-Table | Out-String | Write-Host
