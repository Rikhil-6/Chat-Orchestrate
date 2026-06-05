param(
    [string]$MachineId = "",
    [string]$Backends = "",
    [ValidateSet("true", "false")]
    [string]$DryRun = ""
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $RepoRoot

$Python = ".\.venv\Scripts\python.exe"
if (-not (Test-Path $Python)) {
    $Python = "python"
}

$ArgsList = @("scripts/run_worker.py")
if ($MachineId) {
    $ArgsList += @("--machine-id", $MachineId)
}
if ($Backends) {
    $ArgsList += @("--backends", $Backends)
}
if ($DryRun) {
    $ArgsList += @("--dry-run", $DryRun)
}

& $Python @ArgsList
