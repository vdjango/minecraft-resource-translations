$ErrorActionPreference = "Stop"
$InstallDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Exe = Join-Path $InstallDir "mc-translate.exe"
$EnvFile = Join-Path $InstallDir ".env"
if (-not ([Security.Principal.WindowsPrincipal]::new([Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator))) {
    throw "请以管理员身份打开 PowerShell 再安装服务。"
}
if (-not (Test-Path $EnvFile)) {
    Copy-Item (Join-Path $InstallDir ".env.example") $EnvFile
}
& $Exe service install
if ($LASTEXITCODE -ne 0) { throw "服务注册失败。" }
$Parameters = "HKLM:\SYSTEM\CurrentControlSet\Services\BuggcraftTranslation\Parameters"
New-Item -Path $Parameters -Force | Out-Null
New-ItemProperty -Path $Parameters -Name EnvFile -Value $EnvFile -PropertyType String -Force | Out-Null
& icacls.exe $EnvFile /inheritance:r /grant:r '*S-1-5-18:F' '*S-1-5-32-544:F' | Out-Null
if ($LASTEXITCODE -ne 0) { throw "配置文件权限设置失败。" }
& sc.exe config BuggcraftTranslation start= auto
& sc.exe description BuggcraftTranslation "Buggcraft shared Minecraft resource translation service"
& sc.exe start BuggcraftTranslation
if ($LASTEXITCODE -ne 0) { throw "服务启动失败，请查看 Windows 事件查看器。" }
Write-Host "服务已安装并启动：BuggcraftTranslation"
Write-Host "配置文件：$EnvFile"
Write-Host "首次安装后直接打开网站，任意网页都会进入初始化向导，设置管理员、站点、翻译与资源分发。"
