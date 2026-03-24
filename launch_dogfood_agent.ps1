param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("planner", "reviewer", "operator")]
    [string]$Role,
    [string]$RuntimeDir = ".tmp_dogfood",
    [string]$ReasoningEffort
)

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
if ([System.IO.Path]::IsPathRooted($RuntimeDir)) {
    $runtimePath = $RuntimeDir
}
else {
    $runtimePath = Join-Path $root $RuntimeDir
}

$profileMap = @{
    planner = @{
        ConfigFile = "planner.mailbox_client.json"
        PromptFile = "docs\dogfood-medium-planner-prompt.txt"
        DefaultEffort = "medium"
        ConsumerId = "dogfood-planner-medium"
        LoginArgs = @(
            "--project-id", "mail4agent",
            "--role", "planner",
            "--session", "dogfood",
            "--agent-name", "dogfood-planner"
        )
    }
    reviewer = @{
        ConfigFile = "reviewer.mailbox_client.json"
        PromptFile = "docs\dogfood-medium-reviewer-prompt.txt"
        DefaultEffort = "medium"
        ConsumerId = "dogfood-reviewer-medium"
        LoginArgs = @(
            "--project-id", "mail4agent",
            "--role", "reviewer",
            "--session", "dogfood",
            "--agent-name", "dogfood-reviewer"
        )
    }
    operator = @{
        ConfigFile = "operator.mailbox_client.json"
        PromptFile = "docs\dogfood-high-operator-prompt.txt"
        DefaultEffort = "high"
        ConsumerId = "dogfood-operator-high"
        LoginArgs = @(
            "--project-id", "mail4agent",
            "--local-part", "operator",
            "--mailbox-type", "group",
            "--agent-name", "dogfood-operator"
        )
    }
}

$selected = $profileMap[$Role]
$resolvedEffort = if ($ReasoningEffort) { $ReasoningEffort } else { [string]$selected.DefaultEffort }

$tokenPath = Join-Path $runtimePath "harness.token"
$summaryPath = Join-Path $runtimePath "bootstrap_summary.json"
$configPath = Join-Path $runtimePath ([string]$selected.ConfigFile)
$promptPath = Join-Path $root ([string]$selected.PromptFile)
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
$env:MAILBOX_AGENT_ROLE = $Role
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
    $loginArgs = @(".\client.py", "login", "--output", "token")
    $loginArgs += [string[]]$selected.LoginArgs
    $env:MAILBOX_SESSION_TOKEN = (python @loginArgs).Trim()
    Remove-Item Env:MAILBOX_TOKEN -ErrorAction SilentlyContinue

    Get-Content $promptPath -Raw |
      codex exec -C $root --full-auto --skip-git-repo-check `
        -c model="gpt-5.4" `
        -c model_reasoning_effort="$resolvedEffort" `
        -c approval_policy="never" `
        -c sandbox_mode="workspace-write" `
        -o $lastMessagePath -
}
finally {
    Pop-Location
}
