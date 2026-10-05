# 打包成目录形态 exe：dist\闲鱼AI工作台\闲鱼AI工作台.exe
#   pwsh scripts\build_exe.ps1 [-SkipFrontend]
#
# 并发注意：前端工作流可能在你打包期间重跑 `npm run build`（vite 会先清空 dist），
# 那样 PyInstaller 收 data 时会拿到「文件已消失」的空目录。所以这里先把 dist
# **快照**到 build\frontend_stage，再让 spec 从快照取，避免和前端构建互相踩。
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

$workDir = Join-Path $script:RepoRoot 'build'
$outDir = Join-Path $script:RepoRoot 'dist'

# --- 前端快照（原子化，防止打包中途被前端重构建清空） ---
$stage = Join-Path $workDir 'frontend_stage'
$stageDist = Join-Path $stage 'dist'
if (Test-Path $stage) { Remove-Item -Recurse -Force $stage }
New-Item -ItemType Directory -Force -Path $stage | Out-Null

Write-Step "快照前端产物 -> $stageDist"
Copy-Item -Recurse -Force $dist $stageDist
$stagedIndex = Join-Path $stageDist 'index.html'
if (-not (Test-Path $stagedIndex)) { throw "快照失败，没找到 $stagedIndex" }
$stagedCount = (Get-ChildItem $stageDist -Recurse -File | Measure-Object).Count
$stagedBytes = (Get-ChildItem $stageDist -Recurse -File | Measure-Object -Property Length -Sum).Sum
if ($stagedCount -lt 2) { throw "前端快照文件太少（$stagedCount 个），dist 可能不完整" }
Write-Ok "快照 $stagedCount 个文件, $([math]::Round($stagedBytes/1KB,1)) KB"

Write-Step '检查打包依赖 pyinstaller'
& $script:PythonExe -c "import PyInstaller" 2>$null
if ($LASTEXITCODE -ne 0) {
    throw "未安装 pyinstaller：$script:PythonExe -m pip install pyinstaller"
}

$env:XYWB_REPO_ROOT = $script:RepoRoot
$env:XYWB_FRONTEND_DIST = $stageDist

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

# 校验打包内前端 == 快照（逐文件比对大小 + SHA256，只比大小会漏掉同尺寸的内容改动）
$bundledDir = Join-Path $outDir '闲鱼AI工作台\_internal\frontend\dist'
function Get-DistManifest([string]$root) {
    Get-ChildItem $root -Recurse -File | ForEach-Object {
        $rel = $_.FullName.Substring($root.Length)
        $hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $_.FullName).Hash
        "$rel|$($_.Length)|$hash"
    } | Sort-Object
}
$diff = Compare-Object (Get-DistManifest $stageDist) (Get-DistManifest $bundledDir)
if ($diff) {
    Write-Host ($diff | Format-Table | Out-String)
    throw '打包内前端与快照不一致'
}

$exeBytes = (Get-Item $exe).Length
$totalBytes = (Get-ChildItem (Join-Path $outDir '闲鱼AI工作台') -Recurse -File | Measure-Object -Property Length -Sum).Sum

Write-Ok "exe: $exe"
Write-Ok "exe 大小: $([math]::Round($exeBytes/1MB,2)) MB；产物目录合计 $([math]::Round($totalBytes/1MB,1)) MB"
Write-Ok "打包内前端: $bundledFrontend（与快照逐文件一致）"
Write-Host ''
Write-Host '自检（无窗口模式）：' -ForegroundColor Yellow
Write-Host "  & '$exe' --no-window --port 18080" -ForegroundColor Yellow
Write-Host '  Invoke-WebRequest http://127.0.0.1:18080/api/health -UseBasicParsing' -ForegroundColor Yellow

