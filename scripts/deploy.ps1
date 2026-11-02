# my-cloud-server 部署/启动脚本（Waitress 托管）
# 用法：powershell -ExecutionPolicy Bypass -File scripts\deploy.ps1
$ErrorActionPreference = "Stop"

$serverDir = Split-Path -Parent $PSScriptRoot
$venvDir = Join-Path $serverDir ".venv"
$py = Join-Path $venvDir "Scripts\python.exe"

# 笔记前端源码路径（存在则构建；缺省跳过，仅构建管理后台与云服务 Web）
$noteFrontend = "E:\WebStormProjects\open-note-frontend"
$noteMobile = "E:\WebStormProjects\open-note-mobile"

if (-not (Test-Path $py)) {
    Write-Host "[1/6] Creating virtualenv..."
    python -m venv $venvDir
}

Write-Host "[2/6] Installing requirements..."
& $py -m pip install -r (Join-Path $serverDir "requirements.txt") -q
if ($LASTEXITCODE -ne 0) { Write-Error "pip install failed"; exit 1 }

Write-Host "[3/6] Building admin frontend (Vue3)..."
Push-Location (Join-Path $serverDir "admin-frontend")
try {
    npm install --no-audit --no-fund
    if ($LASTEXITCODE -ne 0) { Write-Error "npm install failed"; exit 1 }
    npm run build
    if ($LASTEXITCODE -ne 0) { Write-Error "npm run build failed"; exit 1 }
} finally {
    Pop-Location
}

Write-Host "[4/6] Building note frontends (desktop/mobile)..."
function Build-NoteFrontend {
    param([string]$Src, [string]$OutDir)
    if (-not (Test-Path (Join-Path $Src "package.json"))) {
        Write-Host "  跳过 $Src（源码不存在）"
        return
    }
    Push-Location $Src
    try {
        npm install --no-audit --no-fund
        if ($LASTEXITCODE -ne 0) { Write-Error "npm install failed: $Src"; exit 1 }
        npm run build
        if ($LASTEXITCODE -ne 0) { Write-Error "npm run build failed: $Src"; exit 1 }
        $dist = Join-Path $Src "dist"
        $target = Join-Path $serverDir $OutDir
        Remove-Item -Recurse -Force $target -ErrorAction SilentlyContinue
        Copy-Item -Recurse $dist $target
        Write-Host "  已构建并复制到 $OutDir"
    } finally {
        Pop-Location
    }
}
Build-NoteFrontend $noteFrontend "static\desktop"
Build-NoteFrontend $noteMobile "static\mobile"

Write-Host "[5/6] Building cloud-service web frontend (portal/album/drive)..."
Push-Location (Join-Path $serverDir "web")
try {
    npm install --no-audit --no-fund
    if ($LASTEXITCODE -ne 0) { Write-Error "npm install failed (web)"; exit 1 }
    npm run build
    if ($LASTEXITCODE -ne 0) { Write-Error "npm run build failed (web)"; exit 1 }
} finally {
    Pop-Location
}

Write-Host "[6/6] Starting my-cloud-server (Waitress on 0.0.0.0:5000)..."
Push-Location $serverDir
try {
    & $py app.py
} finally {
    Pop-Location
}
