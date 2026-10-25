$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Package = Join-Path $Root "dist/package"
$Diagnostics = Join-Path $Root "dist/service-smoke.log"
try {
    & (Join-Path $Package "install-windows-service.ps1")
    $Healthy = $false
    for ($Attempt = 0; $Attempt -lt 40; $Attempt++) {
        try {
            $Health = Invoke-RestMethod -Uri "http://127.0.0.1:18448/health" -TimeoutSec 2
            if ($Health.status -eq "ok") { $Healthy = $true; break }
        } catch { Start-Sleep -Seconds 1 }
    }
    if (-not $Healthy) {
        "Service status:" | Out-File $Diagnostics -Encoding utf8
        Get-CimInstance Win32_Service -Filter "Name='BuggcraftTranslation'" |
            Format-List Name,State,StartMode,StartName,PathName,ExitCode,ProcessId |
            Out-String | Out-File $Diagnostics -Append -Encoding utf8
        "Service registry:" | Out-File $Diagnostics -Append -Encoding utf8
        Get-ItemProperty "HKLM:\SYSTEM\CurrentControlSet\Services\BuggcraftTranslation" -ErrorAction SilentlyContinue |
            Format-List ImagePath,ObjectName,Start,Type | Out-String | Out-File $Diagnostics -Append -Encoding utf8
        "Application events:" | Out-File $Diagnostics -Append -Encoding utf8
        Get-WinEvent -FilterHashtable @{ LogName = "Application"; StartTime = (Get-Date).AddMinutes(-5) } -ErrorAction SilentlyContinue |
            Where-Object { $_.Message -like "*BuggcraftTranslation*" -or $_.ProviderName -eq "Python Service" } |
            Select-Object -First 20 TimeCreated,ProviderName,Id,Message |
            Format-List | Out-String | Out-File $Diagnostics -Append -Encoding utf8
        Get-Content $Diagnostics | Write-Output
        throw "Installed Windows service did not become healthy"
    }
    Stop-Service -Name BuggcraftTranslation
    (Get-Service BuggcraftTranslation).WaitForStatus("Stopped", [TimeSpan]::FromSeconds(30))
    Write-Host "PASS: Windows service installed, served HTTP and stopped cleanly."
} finally {
    & sc.exe stop BuggcraftTranslation 2>$null | Out-Null
    & sc.exe delete BuggcraftTranslation 2>$null | Out-Null
}
