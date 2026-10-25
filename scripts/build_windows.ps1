$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
python -m pip install -e ".[build,windows]"
python -m PyInstaller --noconfirm --clean --onefile --name mc-translate --collect-all mc_translate --hidden-import mc_translate.winservice --hidden-import servicemanager --hidden-import win32serviceutil --hidden-import win32service --hidden-import win32event scripts/windows_entry.py
New-Item -ItemType Directory -Force dist/package | Out-Null
Copy-Item dist/mc-translate.exe dist/package/mc-translate.exe -Force
Copy-Item .env.example dist/package/.env.example -Force
Copy-Item deploy/install-windows-service.ps1 dist/package/install-windows-service.ps1 -Force
Compress-Archive -Path dist/package/* -DestinationPath dist/mc-translate-windows.zip -Force
