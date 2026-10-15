# iOS 虚拟定位轨迹工具 —— Windows 构建脚本
#
# 产出：
#   dist\iOSLocationTrack\iOSLocationTrack.exe        绿色版（直接双击运行）
#   dist\installer\iOSLocationTrack-1.0.0-setup.exe   安装包（需要 Inno Setup 6）
#
# 用法（在项目根目录）：
#     .\scripts\build.ps1              # 打包 exe + 安装包（若装了 Inno Setup）
#     .\scripts\build.ps1 -NoInstaller # 只打包 exe
#     .\scripts\build.ps1 -SkipTests   # 跳过测试
#
# 前置：
#   1. 已运行过 .\scripts\run.ps1（或手动建好 .venv 并装好依赖）
#   2. 图标：resources\AppIcon.ico（缺失时自动用 tools\make_ico.py 生成）
#   3. 开发者镜像：resources\ddi\personalized\Image.dmg（缺失时自动下载）
#   4. Inno Setup 6（可选）：winget install -e --id JRSoftware.InnoSetup

[CmdletBinding()]
param(
    [switch]$NoInstaller,
    [switch]$SkipTests,
    [switch]$SkipIcon,
    [switch]$SkipDdi
)

$ErrorActionPreference = 'Stop'
function Write-Bold { param($m) Write-Host "`n=== $m ===" -ForegroundColor White }
function Write-Ok   { param($m) Write-Host "  [OK] $m" -ForegroundColor Green }
function Write-Warn { param($m) Write-Host "  [!!] $m" -ForegroundColor Yellow }
function Write-Fail { param($m) Write-Host "  [XX] $m" -ForegroundColor Red }

$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
$VenvPy = Join-Path $Root '.venv\Scripts\python.exe'

if (-not (Test-Path $VenvPy)) {
    Write-Fail "找不到 $VenvPy。请先运行 .\scripts\run.ps1 创建环境。"
    exit 1
}

# ---------- 0. 构建依赖 ----------
Write-Bold "0. 构建依赖"
& $VenvPy -c "import PyInstaller, PIL" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Warn "缺少 PyInstaller / Pillow，正在安装…"
    $idx = if ($env:IOSLOC_PIP_INDEX) { $env:IOSLOC_PIP_INDEX } else { 'https://mirrors.aliyun.com/pypi/simple/' }
    & $VenvPy -m pip install --disable-pip-version-check -i $idx pyinstaller pillow
    if ($LASTEXITCODE -ne 0) { Write-Fail "构建依赖安装失败"; exit 1 }
}
Write-Ok "PyInstaller / Pillow 就绪"

# ---------- 1. 图标 ----------
if (-not $SkipIcon) {
    Write-Bold "1. Windows 图标（.ico）"
    if ((Test-Path 'resources\AppIcon.ico') -and
        ((Get-Item 'resources\AppIcon.ico').LastWriteTime -gt (Get-Item 'resources\AppIcon.iconset').LastWriteTime)) {
        Write-Ok "resources\AppIcon.ico 已是最新"
    } else {
        & $VenvPy tools\make_ico.py
        if ($LASTEXITCODE -ne 0) { Write-Fail "图标生成失败"; exit 1 }
    }
}

# ---------- 2. 开发者镜像 ----------
if (-not $SkipDdi) {
    Write-Bold "2. 开发者镜像素材"
    $imageDmg = 'resources\ddi\personalized\Image.dmg'
    if (Test-Path $imageDmg) {
        $mb = [math]::Round((Get-Item $imageDmg).Length / 1MB, 2)
        Write-Ok "$imageDmg 已存在（$mb MB）"
    } else {
        Write-Warn "$imageDmg 缺失（仓库中不入库），正在下载…"
        & $VenvPy tools\fetch_ddi.py --personalized-only
        if (-not (Test-Path $imageDmg)) {
            Write-Fail "镜像下载失败。iOS 17+ 挂载会退化为在线获取，打包产物不完整。"
            Write-Fail "可稍后重试：$VenvPy tools\fetch_ddi.py --personalized-only"
            exit 1
        }
    }
}

# ---------- 3. 测试 ----------
if (-not $SkipTests) {
    Write-Bold "3. 测试（无需设备）"
    $failed = 0
    foreach ($t in @('tests\selftest.py', 'tests\drift_sim.py', 'tests\offline_tiles.py')) {
        Write-Host "  --- $t"
        & $VenvPy $t 2>&1 | Select-Object -Last 3
        if ($LASTEXITCODE -ne 0) { $failed++; Write-Fail "$t 失败" }
    }
    if ($failed -gt 0) { Write-Fail "$failed 个测试套件失败，已中止打包"; exit 1 }
    Write-Ok "全部测试通过"
}

# ---------- 4. PyInstaller ----------
Write-Bold "4. 打包 exe（onedir + requireAdministrator 清单）"
if (Test-Path 'dist\iOSLocationTrack') { Remove-Item 'dist\iOSLocationTrack' -Recurse -Force }
& $VenvPy -m PyInstaller --clean --noconfirm Location.spec
if ($LASTEXITCODE -ne 0) { Write-Fail "PyInstaller 打包失败"; exit 1 }

$exe = 'dist\iOSLocationTrack\iOSLocationTrack.exe'
if (-not (Test-Path $exe)) { Write-Fail "未生成 $exe"; exit 1 }
$sizeMb = [math]::Round(((Get-ChildItem 'dist\iOSLocationTrack' -Recurse -File |
    Measure-Object Length -Sum).Sum / 1MB), 1)
Write-Ok "$exe（整包 $sizeMb MB）"

# ---------- 5. 校验清单里的 requireAdministrator ----------
Write-Bold "5. 校验 exe 是否请求管理员权限"
$manifestOk = $false
try {
    # manifest 嵌在 exe 资源里；用 findstr 直接找 UTF-8 明文即可
    $bytes = [System.IO.File]::ReadAllBytes((Resolve-Path $exe))
    $text = [System.Text.Encoding]::UTF8.GetString($bytes)
    if ($text -match 'requireAdministrator') {
        $manifestOk = $true
        Write-Ok "清单包含 requireAdministrator（启动时会弹 UAC）"
    }
} catch { }
if (-not $manifestOk) {
    Write-Warn "未能在 exe 中确认 requireAdministrator —— 请手动确认 Location.spec 的 uac_admin=True"
}

# ---------- 6. Inno Setup ----------
if ($NoInstaller) {
    Write-Bold "6. 跳过安装包（-NoInstaller）"
} else {
    Write-Bold "6. 制作安装包（Inno Setup）"
    $iscc = $null
    foreach ($candidate in @(
        "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
        "$env:ProgramFiles\Inno Setup 6\ISCC.exe",
        "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe",
        "$env:LOCALAPPDATA\Inno Setup 6\ISCC.exe"
    )) {
        if ($candidate -and (Test-Path $candidate)) { $iscc = $candidate; break }
    }
    if (-not $iscc) {
        $cmd = Get-Command ISCC.exe -ErrorAction SilentlyContinue
        if ($cmd) { $iscc = $cmd.Source }
    }
    if (-not $iscc) {
        Write-Warn "未找到 Inno Setup 6，跳过安装包。"
        Write-Warn "安装：winget install -e --id JRSoftware.InnoSetup"
        Write-Warn "然后重跑：.\scripts\build.ps1 -SkipTests -SkipIcon -SkipDdi"
    } else {
        Write-Host "  使用 $iscc"
        & $iscc 'installer\iosloc.iss'
        if ($LASTEXITCODE -ne 0) { Write-Fail "Inno Setup 编译失败"; exit 1 }
        Get-ChildItem 'dist\installer\*.exe' | ForEach-Object {
            Write-Ok "$($_.FullName)（$([math]::Round($_.Length/1MB,1)) MB）"
        }
    }
}

Write-Bold "完成"
Write-Host "  绿色版：dist\iOSLocationTrack\iOSLocationTrack.exe"
if (Test-Path 'dist\installer') {
    Get-ChildItem 'dist\installer\*.exe' -ErrorAction SilentlyContinue | ForEach-Object {
        Write-Host "  安装包：$($_.FullName)"
    }
}
Write-Host "`n  提示：exe 启动时会请求管理员权限（用于首次自动安装 Apple Mobile Device Support）。"
