param(
    [string]$Python = 'E:\code\.venv\Scripts\python.exe',
    [string]$CacheRoot = 'G:\BuggcraftMirror',
    [string]$EnvFile = '.env'
)
$ErrorActionPreference = 'Stop'
$repo = Split-Path $PSScriptRoot -Parent
$pythonPath = (Resolve-Path -LiteralPath $Python).Path
if (Get-NetTCPConnection -LocalPort 18449 -State Listen -ErrorAction SilentlyContinue) {
    throw 'Port 18449 already has a listener. Reuse or stop that mirror before starting another.'
}
New-Item -ItemType Directory -Force -Path $CacheRoot | Out-Null
$cachePath = (Resolve-Path -LiteralPath $CacheRoot).Path
$env:PYTHONPATH = Join-Path $repo 'src'
$env:PYTHONIOENCODING = 'utf-8'
$process = Start-Process -FilePath $pythonPath -ArgumentList @('-u', '-m', 'mc_translate.mirror', '--root', ('"' + $cachePath + '"'), '--env', ('"' + $EnvFile + '"')) -WorkingDirectory $repo -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $cachePath 'mirror.stdout.log') -RedirectStandardError (Join-Path $cachePath 'mirror.stderr.log')
$listener = $null
for ($attempt = 0; $attempt -lt 20; $attempt++) {
    Start-Sleep -Milliseconds 500
    if ($process.HasExited) { throw 'Mirror exited during startup; inspect mirror.stderr.log.' }
    $listener = Get-NetTCPConnection -LocalPort 18449 -State Listen -ErrorAction SilentlyContinue
    if ($listener) { break }
}
if (-not $listener) { throw 'Mirror did not open port 18449; inspect mirror.stderr.log.' }
$listener.OwningProcess | Set-Content -LiteralPath (Join-Path $cachePath 'mirror.pid')
Write-Output "Mirror PID $($listener.OwningProcess); loopback http://127.0.0.1:18449 ; cache $cachePath"
