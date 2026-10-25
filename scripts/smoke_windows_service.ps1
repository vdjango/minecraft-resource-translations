$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Package = Join-Path $Root "dist/package"
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
        Get-WinEvent -FilterHashtable @{ LogName = "Application"; StartTime = (Get-Date).AddMinutes(-5) } -ErrorAction SilentlyContinue |
            Where-Object { $_.Message -like "*BuggcraftTranslation*" -or $_.ProviderName -eq "Python Service" } |
            Select-Object -First 10 Message | Write-Output
        throw "Installed Windows service did not become healthy"
    }
    Stop-Service -Name BuggcraftTranslation
    (Get-Service BuggcraftTranslation).WaitForStatus("Stopped", [TimeSpan]::FromSeconds(30))
    Write-Host "PASS: Windows service installed, served HTTP and stopped cleanly."
} finally {
    & sc.exe stop BuggcraftTranslation 2>$null | Out-Null
    & sc.exe delete BuggcraftTranslation 2>$null | Out-Null
}
