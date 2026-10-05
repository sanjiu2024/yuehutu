# 打包成目录形态 exe：dist\闲鱼AI工作台\闲鱼AI工作台.exe
#   pwsh scripts\build_exe.ps1 [-SkipFrontend]
param(
    [switch]$SkipFrontend
)

. (Join-Path $PSScriptRoot '_common.ps1')
Assert-Python

$dist = Join-Path $script:RepoRoot 'frontend\dist'
$index = Join-Path $dist 'index.html'

if (-not $SkipFrontend -or -not (Test-Path $index)) {
    Write-Step '构建前端'
    & (Join-Path $PSScriptRoot 'build_frontend.ps1')
    if ($LASTEXITCODE -ne 0) { throw "build_frontend.ps1 失败（exit $LASTEXITCODE）" }
}

if (-not (Test-Path $index)) {
    throw "前端产物缺失: $index（先跑 pwsh scripts\build_frontend.ps1）"
}

Write-Step '检查打包依赖 pyinstaller'
& $script:PythonExe -c "import PyInstaller" 2>$null
if ($LASTEXITCODE -ne 0) {
    throw "未安装 pyinstaller：$script:PythonExe -m pip install pyinstaller"
}

$env:XYWB_REPO_ROOT = $script:RepoRoot
$workDir = Join-Path $script:RepoRoot 'build'
$outDir = Join-Path $script:RepoRoot 'dist'

Write-Step 'PyInstaller（onedir）'
Push-Location $script:RepoRoot
try {
    & $script:PythonExe -m PyInstaller --noconfirm --clean `
        --workpath $workDir --distpath $outDir `
        (Join-Path $script:RepoRoot 'desktop\desktop.spec')
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller 失败（exit $LASTEXITCODE）" }
} finally {
    Pop-Location
}

$exe = Join-Path $outDir '闲鱼AI工作台\闲鱼AI工作台.exe'
if (-not (Test-Path $exe)) { throw "打包结束但没找到 $exe" }

$bundledFrontend = Join-Path $outDir '闲鱼AI工作台\_internal\frontend\dist\index.html'
if (-not (Test-Path $bundledFrontend)) { throw "产物里没有前端: $bundledFrontend" }

Write-Ok "exe: $exe"
Write-Ok "打包内前端: $bundledFrontend"
Write-Host ''
Write-Host '自检（无窗口模式）：' -ForegroundColor Yellow
Write-Host "  & '$exe' --no-window --port 18080" -ForegroundColor Yellow
Write-Host '  Invoke-WebRequest http://127.0.0.1:18080/api/health -UseBasicParsing' -ForegroundColor Yellow
