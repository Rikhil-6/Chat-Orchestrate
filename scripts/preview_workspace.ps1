param(
    [string]$Workspace = "default",
    [string]$HostName = "127.0.0.1",
    [int]$FrontendPort = 5173,
    [int]$BackendPort = 8000
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $RepoRoot

$Python = ".\.venv\Scripts\python.exe"
if (-not (Test-Path $Python)) {
    $Python = "python"
}

& $Python scripts/preview_workspace.py --workspace $Workspace --host $HostName --frontend-port $FrontendPort --backend-port $BackendPort
