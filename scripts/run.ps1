# iOS 虚拟定位轨迹工具 —— Windows 一键启动脚本
#
# 用法（在项目根目录）：
#     .\scripts\run.ps1                 # 正常启动
#     .\scripts\run.ps1 -Mock           # 无设备模拟模式
#     .\scripts\run.ps1 -CheckDriver    # 只检查 Apple Mobile Device Support
#     .\scripts\run.ps1 -InstallDriver  # 检查并安装 Apple 驱动
#     .\scripts\run.ps1 -NoBrowser      # 不自动打开界面
#
# 如果提示"禁止运行脚本"，先执行：
#     Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass

[CmdletBinding()]
param(
    [switch]$Mock,
    [switch]$CheckDriver,
    [switch]$InstallDriver,
    [switch]$NoBrowser,
    [switch]$NoDriverCheck,
    [int]$Port = 0,
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Rest
)

$ErrorActionPreference = 'Stop'

function Write-Info { param($m) Write-Host "  $m" }
function Write-Bold { param($m) Write-Host $m -ForegroundColor White }
function Write-Warn { param($m) Write-Host "  $m" -ForegroundColor Yellow }
function Write-Fail { param($m) Write-Host "  $m" -ForegroundColor Red }
function Write-Ok   { param($m) Write-Host "  $m" -ForegroundColor Green }

$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
$VenvPy = Join-Path $Root '.venv\Scripts\python.exe'

# 国内网络默认走阿里云镜像（可用 IOSLOC_PIP_INDEX 覆盖）
$PipIndex = if ($env:IOSLOC_PIP_INDEX) { $env:IOSLOC_PIP_INDEX } else { 'https://mirrors.aliyun.com/pypi/simple/' }

Write-Bold "iOS 虚拟定位轨迹工具（Windows）"
Write-Info "目录：$Root"

# ---------- 1. Python ----------
if (-not (Test-Path $VenvPy)) {
    Write-Info "首次运行：创建虚拟环境…"
    $python = $null
    foreach ($candidate in @('py -3.12', 'py -3.13', 'py -3.11', 'python')) {
        $parts = $candidate.Split(' ')
        $exe = Get-Command $parts[0] -ErrorAction SilentlyContinue
        if ($exe) { $python = $candidate; break }
    }
    if (-not $python) {
        Write-Fail "未找到 Python 3.11+。请先安装：https://www.python.org/downloads/windows/"
        Write-Fail "安装时请勾选「Add python.exe to PATH」。"
        exit 1
    }
    Write-Info "使用 $python 创建虚拟环境…"
    Invoke-Expression "$python -m venv `"$Root\.venv`""
    if (-not (Test-Path $VenvPy)) { Write-Fail "虚拟环境创建失败"; exit 1 }
}
Write-Info "Python：$(& $VenvPy -V 2>&1)"

# ---------- 2. 依赖 ----------
$needInstall = $false
& $VenvPy -c "import pymobiledevice3" 2>$null
if ($LASTEXITCODE -ne 0) { $needInstall = $true }

if ($needInstall) {
    Write-Bold "首次运行：安装依赖（约 1-3 分钟，走国内镜像）"
    & $VenvPy -m pip install --quiet --disable-pip-version-check -i $PipIndex pymobiledevice3
    if ($LASTEXITCODE -ne 0) {
        Write-Warn "国内镜像失败，改用官方源重试…"
        & $VenvPy -m pip install --quiet --disable-pip-version-check pymobiledevice3
        if ($LASTEXITCODE -ne 0) {
            Write-Fail "依赖安装失败。请手动执行："
            Write-Fail "  $VenvPy -m pip install -i $PipIndex pymobiledevice3"
            exit 1
        }
    }
    Write-Ok "依赖安装完成"
}
Write-Info "pymobiledevice3：$(& $VenvPy -c "import pymobiledevice3;print('已就绪')" 2>$null)"

# ---------- 3. 开发者镜像素材（不入库，需要时下载）----------
$imageDmg = Join-Path $Root 'resources\ddi\personalized\Image.dmg'
if (-not (Test-Path $imageDmg)) {
    Write-Warn "缺少开发者镜像 Image.dmg（约 15MB，仓库中不入库），正在下载…"
    & $VenvPy (Join-Path $Root 'tools\fetch_ddi.py') --personalized-only
    if (-not (Test-Path $imageDmg)) {
        Write-Warn "镜像下载失败：iOS 17+ 挂载会尝试在线获取，可能较慢。"
    }
}

# ---------- 4. 组装参数 ----------
$mainArgs = @((Join-Path $Root 'main.py'))
if ($Mock)          { $mainArgs += '--mock' }
if ($CheckDriver)   { $mainArgs += '--check-driver' }
if ($InstallDriver) { $mainArgs += '--install-driver' }
if ($NoBrowser)     { $mainArgs += '--no-browser' }
if ($NoDriverCheck) { $mainArgs += '--no-driver-check' }
if ($Port -gt 0)    { $mainArgs += @('--port', $Port) }
if ($Rest)          { $mainArgs += $Rest }

# ---------- 5. 启动 ----------
& $VenvPy @mainArgs
exit $LASTEXITCODE
