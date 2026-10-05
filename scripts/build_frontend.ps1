# 构建前端：frontend/dist（后端会自动静态托管它）
#   pwsh scripts\build_frontend.ps1 [-SkipInstall]
param(
    [switch]$SkipInstall,   # 完全跳过 npm install
    [switch]$Reinstall      # 强制重装依赖
)

. (Join-Path $PSScriptRoot '_common.ps1')

$frontend = Join-Path $script:RepoRoot 'frontend'
$dist = Join-Path $frontend 'dist'

if (-not (Test-Path (Join-Path $frontend 'package.json'))) {
    throw "找不到 $frontend\package.json"
}

Push-Location $frontend
try {
    $nodeModules = Join-Path $frontend 'node_modules'
    $needInstall = $Reinstall -or (-not $SkipInstall -and -not (Test-Path $nodeModules))
    if ($SkipInstall -and -not (Test-Path $nodeModules)) {
        throw "node_modules 不存在，不能 -SkipInstall（先跑一次不带参数的 build_frontend.ps1）"
    }
    if ($needInstall) {
        Write-Step 'npm install（--no-fund --no-audit）'
        npm install --no-fund --no-audit
        if ($LASTEXITCODE -ne 0) { throw "npm install 失败（exit $LASTEXITCODE）" }
    } else {
        Write-Step 'node_modules 已存在，跳过 npm install（要强制重装加 -Reinstall）'
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
