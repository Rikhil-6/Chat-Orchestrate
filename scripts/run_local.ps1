param(
    [string]$HostName = "127.0.0.1",
    [int]$Port = 7862,
    [string]$MachineId = "",
    [string]$Backends = "",
    [switch]$NoKeepAlive
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $RepoRoot

$Python = ".\.venv\Scripts\python.exe"
if (-not (Test-Path $Python)) {
    $Python = "python"
}

$ArgsList = @("scripts/run_local.py", "--host", $HostName, "--port", "$Port")
if ($MachineId) {
    $ArgsList += @("--machine-id", $MachineId)
}
if ($Backends) {
    $ArgsList += @("--backends", $Backends)
}
if ($NoKeepAlive) {
    $ArgsList += "--no-keepalive"
}

& $Python @ArgsList
