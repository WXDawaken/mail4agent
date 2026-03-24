param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("planner", "reviewer")]
    [string]$Role,
    [string]$RuntimeDir = ".tmp_dogfood"
)

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
if ([System.IO.Path]::IsPathRooted($RuntimeDir)) {
    $runtimePath = $RuntimeDir
}
else {
    $runtimePath = Join-Path $root $RuntimeDir
}
$tokenPath = Join-Path $runtimePath "harness.token"
$summaryPath = Join-Path $runtimePath "bootstrap_summary.json"
$configPath = Join-Path $runtimePath "$Role.mailbox_client.json"
$promptPath = Join-Path $root "docs\dogfood-medium-$Role-prompt.txt"
$lastMessagePath = Join-Path $runtimePath "$Role-last-message.txt"
$codexHome = Join-Path $root ".codex_home_dogfood"
$globalCodexHome = Join-Path $env:USERPROFILE ".codex"

if (-not (Test-Path $tokenPath)) {
    throw "Missing $tokenPath. Run dogfood_smoke_bootstrap.py first."
}
if (-not (Test-Path $summaryPath)) {
    throw "Missing $summaryPath. Run dogfood_smoke_bootstrap.py first."
}
if (-not (Test-Path $configPath)) {
    throw "Missing $configPath. Run dogfood_smoke_bootstrap.py first."
}
if (-not (Test-Path $promptPath)) {
    throw "Missing $promptPath."
}

$summary = Get-Content $summaryPath -Raw | ConvertFrom-Json
$env:MAILBOX_TOKEN = (Get-Content $tokenPath -Raw).Trim()
$env:MAILBOX_CONFIG = $configPath
$env:MAILBOX_TIMEOUT_SECONDS = "15"
$env:MAILBOX_HARNESS_ID = [string]$summary.harness_id
$env:MAILBOX_PROJECT_ID = [string]$summary.project_id
$env:CODEX_HOME = $codexHome

New-Item -ItemType Directory -Force -Path $codexHome | Out-Null
foreach ($name in @("auth.json", "config.toml", "cap_sid", "version.json")) {
    $sourcePath = Join-Path $globalCodexHome $name
    if (Test-Path $sourcePath) {
        Copy-Item $sourcePath (Join-Path $codexHome $name) -Force
    }
}

Push-Location $root
try {
    $env:MAILBOX_SESSION_TOKEN = (
        python .\client.py login --output token --project-id ([string]$summary.project_id) --role $Role --session dogfood --agent-name ("dogfood-" + $Role)
    ).Trim()
    Remove-Item Env:MAILBOX_TOKEN -ErrorAction SilentlyContinue

    Get-Content $promptPath -Raw |
      codex exec -C $root --full-auto --skip-git-repo-check `
        -c model="gpt-5.4" `
        -c model_reasoning_effort="medium" `
        -c approval_policy="never" `
        -c sandbox_mode="workspace-write" `
        -o $lastMessagePath -
}
finally {
    Pop-Location
}
