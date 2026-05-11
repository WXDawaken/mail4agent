param(
    [ValidateSet("operator", "plugin_dev", "core_dev", "salvage_run_dev", "game_engine_dev")]
    [string]$Role = "operator",
    [string]$RuntimeDir = ".tmp_dogfood",
    [ValidateSet("codex-cli", "app-server")]
    [string]$Backend = "codex-cli",
    [double]$IdleExitAfterSeconds = 0,
    [double]$WorkerIdleTimeoutSeconds = -1,
    [double]$WorkerMaxAgeSeconds = -1,
    [string]$ConsumerId = "",
    [string]$WorkspaceDir = "",
    [string]$CodexHomeDir = ""
)

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$args = @(
    ".\mailbox_oncall_server.py",
    "--role", $Role,
    "--runtime-dir", $RuntimeDir,
    "--backend", $Backend
)

if ($IdleExitAfterSeconds -gt 0) {
    $args += @("--idle-exit-after-seconds", [string]$IdleExitAfterSeconds)
}
if ($WorkerIdleTimeoutSeconds -ge 0) {
    $args += @("--worker-idle-timeout-seconds", [string]$WorkerIdleTimeoutSeconds)
}
if ($WorkerMaxAgeSeconds -ge 0) {
    $args += @("--worker-max-age-seconds", [string]$WorkerMaxAgeSeconds)
}
if (-not [string]::IsNullOrWhiteSpace($ConsumerId)) {
    $args += @("--consumer-id", $ConsumerId)
}
if (-not [string]::IsNullOrWhiteSpace($WorkspaceDir)) {
    $args += @("--codex-workspace-dir", $WorkspaceDir)
}
if (-not [string]::IsNullOrWhiteSpace($CodexHomeDir)) {
    $args += @("--codex-home-dir", $CodexHomeDir)
}

Push-Location $root
try {
    python @args
}
finally {
    Pop-Location
}
