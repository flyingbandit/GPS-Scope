# GPS Scope updater - run from this folder:
#   powershell -ExecutionPolicy Bypass -File .\update.ps1
#   (or name the Pi:  ... -File .\update.ps1 -Pi pi@192.168.1.50)
# The Pi defaults to pi@raspberrypi.local. To change the default, put user@host on one line
# in a file called pi-target.txt next to this script (it's ignored by git).
param([string]$Pi = "")
$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not $Pi) {
  $t = Join-Path $here "pi-target.txt"
  if (Test-Path $t) { $Pi = (Get-Content $t -TotalCount 1).Trim() } else { $Pi = "pi@raspberrypi.local" }
}
$PiHost = $Pi.Split("@")[-1]

Write-Host "1/4  Making the Pi's gpsscope folder writable (scp from Windows makes it read-only)" -ForegroundColor Cyan
ssh $Pi "mkdir -p ~/gpsscope/static && chmod -R u+rwX ~/gpsscope"
if ($LASTEXITCODE -ne 0) { throw "ssh failed - is the Pi on and is '$Pi' right?" }

Write-Host "2/4  Copying files" -ForegroundColor Cyan
$files = @("gpsscope.py","analysis.py","analyse_run.py","share.py","README.md","install.sh","requirements.txt") | ForEach-Object { Join-Path $here $_ }
scp @files "${Pi}:~/gpsscope/"
if ($LASTEXITCODE -ne 0) { throw "copying the program files failed" }
scp (Join-Path $here "static\index.html") "${Pi}:~/gpsscope/static/"
if ($LASTEXITCODE -ne 0) { throw "copying index.html failed" }

Write-Host "3/4  Restarting GPS Scope (may ask for the Pi password for sudo)" -ForegroundColor Cyan
ssh -t $Pi "chmod -R u+rwX ~/gpsscope && sed -i 's/\r$//' ~/gpsscope/install.sh && sudo systemctl restart gpsscope"

Write-Host "4/4  Checking what's running" -ForegroundColor Cyan
Start-Sleep -Seconds 3
ssh $Pi "grep -m1 -o 'GPSSCOPE_VERSION = .*' ~/gpsscope/gpsscope.py; grep -m1 -o 'PAGE_VERSION = .*' ~/gpsscope/static/index.html; systemctl is-active gpsscope; journalctl -u gpsscope -n 5 --no-pager"
Write-Host "Done. Open http://${PiHost}:8080 and press Ctrl+F5 once - the header shows the version." -ForegroundColor Green
