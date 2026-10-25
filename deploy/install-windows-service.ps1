$ErrorActionPreference = "Stop"
$InstallDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Exe = Join-Path $InstallDir "mc-translate.exe"
$EnvFile = Join-Path $InstallDir ".env"
if (-not (Test-Path $EnvFile)) {
    Copy-Item (Join-Path $InstallDir ".env.example") $EnvFile
    $bytes = New-Object byte[] 32
    $rng = [Security.Cryptography.RandomNumberGenerator]::Create()
    $rng.GetBytes($bytes)
    $rng.Dispose()
    $token = "bct_admin_" + [Convert]::ToBase64String($bytes).Replace("+", "-").Replace("/", "_").TrimEnd("=")
    (Get-Content $EnvFile -Raw) -replace "TRANSLATION_ADMIN_TOKEN=.*", "TRANSLATION_ADMIN_TOKEN=$token" | Set-Content $EnvFile -Encoding utf8
}
[Environment]::SetEnvironmentVariable("TRANSLATION_ENV_FILE", $EnvFile, "Machine")
& $Exe service install
& sc.exe config BuggcraftTranslation start= auto
& sc.exe description BuggcraftTranslation "Buggcraft shared Minecraft resource translation service"
& sc.exe start BuggcraftTranslation
Write-Host "服务已安装并启动：BuggcraftTranslation"
Write-Host "配置文件：$EnvFile"
