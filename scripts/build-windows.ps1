Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location -LiteralPath $root

python -m PyInstaller `
  --noconfirm `
  --windowed `
  --name GridLens `
  --icon ".\assets\icon.ico" `
  --add-data ".\assets\icon.ico;assets" `
  --add-data ".\assets\icon.png;assets" `
  ".\GridLens.pyw"
