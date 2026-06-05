param(
    [switch]$Dev
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $RepoRoot

if (-not (Test-Path ".venv")) {
    python -m venv .venv
}

& ".\.venv\Scripts\python.exe" -m pip install --upgrade pip

if ($Dev) {
    & ".\.venv\Scripts\python.exe" -m pip install -e ".[dev]"
} else {
    & ".\.venv\Scripts\python.exe" -m pip install -e .
}

if (-not (Test-Path ".env") -and (Test-Path ".env.example")) {
    Copy-Item ".env.example" ".env"
}

Write-Host "Setup complete."
Write-Host "Run the UI with: .\scripts\run_local.ps1"
