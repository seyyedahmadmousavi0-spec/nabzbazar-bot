# Windows PowerShell: first-time setup. Run from the project root.
$ErrorActionPreference = "Stop"
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
if (-not (Test-Path .env)) { Copy-Item .env.example .env; Write-Host "Created .env - edit it (never commit it)." }
.\.venv\Scripts\python.exe -m marketintel init-db
Write-Host "Done. Try: .\.venv\Scripts\python.exe -m marketintel demo"
