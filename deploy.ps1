# Copies Who's Home to the Pi and sets it running as a service.
# Run from this folder in PowerShell:  .\deploy.ps1 -Pi pi@raspberrypi.local
# Change -Pi to whatever you type after ssh to reach your Pi.
# Safe to run again after any change: it copies the files over and restarts.

param(
    [string]$Pi = "pi@raspberrypi.local",
    [string]$Folder = "whos-home"
)

$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path

if (-not (Test-Path "$here\config.py")) {
    Copy-Item "$here\config.example.py" "$here\config.py"
    Write-Host "Made config.py from config.example.py. Add your ntfy topic to it, then run this again." -ForegroundColor Yellow
    exit
}

Write-Host "Copying files to $Pi ..." -ForegroundColor Cyan
ssh $Pi "mkdir -p ~/$Folder/fonts"
scp "$here\whoshome.py" "$here\discover.py" "$here\config.py" "$here\whoshome.service" "$here\setup.sh" "$here\oui.tsv" "${Pi}:~/$Folder/"
scp "$here\fonts\*.woff2" "${Pi}:~/$Folder/fonts/"

Write-Host "Installing and starting (it may ask for the Pi's password) ..." -ForegroundColor Cyan
ssh -t $Pi "sed -i 's/\r$//' ~/$Folder/setup.sh && bash ~/$Folder/setup.sh"

Write-Host "Done." -ForegroundColor Green
