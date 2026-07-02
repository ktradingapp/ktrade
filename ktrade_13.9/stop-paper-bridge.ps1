# KTrade PRO - Stop the paper trading bridge (port 5001)
$ErrorActionPreference = "Stop"

$connection = Get-NetTCPConnection -LocalPort 5001 -State Listen -ErrorAction SilentlyContinue
if (-not $connection) {
    Write-Host "[KTrade] Bridge is not running on port 5001." -ForegroundColor Yellow
    exit 0
}
Stop-Process -Id $connection.OwningProcess -Force
Write-Host "[KTrade] Paper bridge stopped." -ForegroundColor Green
