$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

Write-Host "[1/4] 创建虚拟环境 .venv"
python -m venv .venv

Write-Host "[2/4] 安装 Python 依赖"
& .\.venv\Scripts\python.exe -m pip install --upgrade pip
& .\.venv\Scripts\python.exe -m pip install -r requirements.txt

Write-Host "[3/4] 安装 Chromium（约 150MB，仅首次）"
& .\.venv\Scripts\python.exe -m playwright install chromium

Write-Host "[4/4] 生成配置文件"
if (-not (Test-Path .env)) { Copy-Item .env.example .env }

Write-Host ""
Write-Host "安装完成。启动服务："
Write-Host "  .\.venv\Scripts\Activate.ps1"
Write-Host "  python manage.py start"
Write-Host "  python manage.py status"
