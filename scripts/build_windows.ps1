$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
python -m pip install -e ".[build,windows]"
if ($LASTEXITCODE -ne 0) { throw "Dependency installation failed" }
python -m PyInstaller --noconfirm --clean --onefile --name mc-translate --collect-all mc_translate --hidden-import mc_translate.winservice --hidden-import servicemanager --hidden-import win32serviceutil --hidden-import win32service --hidden-import win32event scripts/windows_entry.py
if ($LASTEXITCODE -ne 0) { throw "Executable build failed" }
New-Item -ItemType Directory -Force dist/package | Out-Null
Copy-Item dist/mc-translate.exe dist/package/mc-translate.exe -Force
Copy-Item .env.example dist/package/.env.example -Force
Copy-Item deploy/install-windows-service.ps1 dist/package/install-windows-service.ps1 -Force
python -c "from pathlib import Path; from zipfile import ZipFile,ZIP_DEFLATED; root=Path('dist/package'); archive=ZipFile('dist/mc-translate-windows.zip','w',ZIP_DEFLATED); [archive.write(p,p.name) for p in root.iterdir() if p.is_file()]; archive.close()"
