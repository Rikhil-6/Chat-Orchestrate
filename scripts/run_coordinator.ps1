param(
    [string]$HostName = "",
    [int]$Port = 0,
    [string]$ClusterId = "",
    [string]$Token = "",
    [string]$StatePath = ""
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $RepoRoot

$Python = ".\.venv\Scripts\python.exe"
if (-not (Test-Path $Python)) {
    $Python = "python"
}

$ArgsList = @("scripts/run_coordinator.py")
if ($HostName) {
    $ArgsList += @("--host", $HostName)
}
if ($Port -gt 0) {
    $ArgsList += @("--port", "$Port")
}
if ($ClusterId) {
    $ArgsList += @("--cluster-id", $ClusterId)
}
if ($Token) {
    $ArgsList += @("--token", $Token)
}
if ($StatePath) {
    $ArgsList += @("--state-path", $StatePath)
}

& $Python @ArgsList
