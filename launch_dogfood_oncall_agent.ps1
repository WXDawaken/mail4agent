param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("operator", "plugin_dev", "core_dev", "salvage_run_dev", "game_engine_dev")]
    [string]$Role,
    [string]$RuntimeDir = ".tmp_dogfood",
    [string]$ReasoningEffort,
    [string]$WorkspaceDir = "",
    [string]$CodexHomeDir = ""
)

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$mailboxClientPath = Join-Path $root "client.py"
$workspaceRoot = if ([string]::IsNullOrWhiteSpace($WorkspaceDir)) {
    $root
}
elseif ([System.IO.Path]::IsPathRooted($WorkspaceDir)) {
    $WorkspaceDir
}
else {
    Join-Path $root $WorkspaceDir
}
if ([System.IO.Path]::IsPathRooted($RuntimeDir)) {
    $runtimePath = $RuntimeDir
}
else {
    $runtimePath = Join-Path $root $RuntimeDir
}

$deliveryJson = [Console]::In.ReadToEnd()
if ([string]::IsNullOrWhiteSpace($deliveryJson)) {
    throw "Expected claimed delivery JSON on stdin."
}

$profileMap = @{
    operator = @{
        ConfigFile = "operator.mailbox_client.json"
        PromptFile = "docs\dogfood-high-operator-oncall-prompt.txt"
        DefaultEffort = "high"
        LoginArgs = @(
            "--project-id", "mail4agent",
            "--local-part", "operator",
            "--mailbox-type", "group",
            "--agent-name", "dogfood-operator"
        )
    }
    plugin_dev = @{
        ConfigFile = "plugin_dev.mailbox_client.json"
        PromptFile = "docs\anchor-agent-plugin-dev-oncall-prompt.txt"
        DefaultEffort = "high"
    }
    core_dev = @{
        ConfigFile = "core_dev.mailbox_client.json"
        PromptFile = "docs\anchor-agent-core-dev-oncall-prompt.txt"
        DefaultEffort = "high"
    }
    salvage_run_dev = @{
        ConfigFile = "salvage_run_dev.mailbox_client.json"
        PromptFile = "docs\salvage-run-dev-oncall-prompt.txt"
        DefaultEffort = "high"
    }
    game_engine_dev = @{
        ConfigFile = "game_engine_dev.mailbox_client.json"
        PromptFile = "docs\game-engine-dev-oncall-prompt.txt"
        DefaultEffort = "high"
    }
}

$selected = $profileMap[$Role]
$resolvedEffort = if ($ReasoningEffort) { $ReasoningEffort } else { [string]$selected.DefaultEffort }

$tokenPath = Join-Path $runtimePath "harness.token"
$summaryPath = Join-Path $runtimePath "bootstrap_summary.json"
$configPath = Join-Path $runtimePath ([string]$selected.ConfigFile)
$promptPath = Join-Path $workspaceRoot ([string]$selected.PromptFile)
$lastMessagePath = Join-Path $runtimePath "$Role-oncall-last-message.txt"
$deliveryPath = Join-Path $runtimePath "$Role-current-delivery.json"
$sandboxRuntimeDir = Join-Path $workspaceRoot (Join-Path ".tmp_dogfood_live" "$Role-oncall")
$sandboxConfigPath = Join-Path $sandboxRuntimeDir ([string]$selected.ConfigFile)
$sandboxDeliveryPath = Join-Path $sandboxRuntimeDir "$Role-current-delivery.json"
$codexHome = if ([string]::IsNullOrWhiteSpace($CodexHomeDir)) {
    Join-Path $workspaceRoot ".codex_home_dogfood"
}
elseif ([System.IO.Path]::IsPathRooted($CodexHomeDir)) {
    $CodexHomeDir
}
else {
    Join-Path $workspaceRoot $CodexHomeDir
}
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

[System.IO.File]::WriteAllText(
    $deliveryPath,
    $deliveryJson,
    (New-Object System.Text.UTF8Encoding($false))
)
$delivery = $deliveryJson | ConvertFrom-Json
$deliveryId = [string]$delivery.delivery_id
$messageId = [string]$delivery.message_id
$threadId = [string]$delivery.thread_id
$claimToken = [string]$delivery.claim_token
$deliveryFrom = [string]$delivery.from
$deliveryTo = [string]$delivery.to

$summary = Get-Content $summaryPath -Raw | ConvertFrom-Json
$configJson = Get-Content $configPath -Raw | ConvertFrom-Json
$env:MAILBOX_TOKEN = (Get-Content $tokenPath -Raw).Trim()
$env:MAILBOX_TIMEOUT_SECONDS = "15"
$env:MAILBOX_HARNESS_ID = [string]$summary.harness_id
$env:MAILBOX_PROJECT_ID = if ($configJson.project_id) { [string]$configJson.project_id } else { [string]$summary.project_id }
$env:MAILBOX_AGENT_ROLE = $Role
$env:MAILBOX_ONCALL_MODE = "1"
$env:MAILBOX_DELIVERY_ID = $deliveryId
$env:MAILBOX_MESSAGE_ID = $messageId
$env:MAILBOX_THREAD_ID = $threadId
$env:MAILBOX_CLAIM_TOKEN = $claimToken
$env:MAILBOX_DELIVERY_FROM_ADDRESS = $deliveryFrom
$env:MAILBOX_DELIVERY_TO_ADDRESS = $deliveryTo
$env:CODEX_HOME = $codexHome

Remove-Item Env:MAILBOX_FROM_ADDRESS -ErrorAction SilentlyContinue
Remove-Item Env:MAILBOX_INBOX_ADDRESS -ErrorAction SilentlyContinue

New-Item -ItemType Directory -Force -Path $codexHome | Out-Null
New-Item -ItemType Directory -Force -Path $sandboxRuntimeDir | Out-Null
Copy-Item $configPath $sandboxConfigPath -Force
[System.IO.File]::WriteAllText(
    $sandboxDeliveryPath,
    $deliveryJson,
    (New-Object System.Text.UTF8Encoding($false))
)
$env:MAILBOX_CONFIG = $sandboxConfigPath
$env:MAILBOX_DELIVERY_FILE = $sandboxDeliveryPath
foreach ($name in @("auth.json", "config.toml", "cap_sid", "version.json")) {
    $sourcePath = Join-Path $globalCodexHome $name
    if (Test-Path $sourcePath) {
        Copy-Item $sourcePath (Join-Path $codexHome $name) -Force
    }
}

$oncallContext = @"

Claimed delivery context:
- delivery_id: $deliveryId
- message_id: $messageId
- thread_id: $threadId
- from_address: $deliveryFrom
- to_address: $deliveryTo
- delivery_file: $sandboxDeliveryPath

Oncall rules:
1. The supervisor already claimed this delivery. Do not run python "$mailboxClientPath" claim.
2. Read the thread with python "$mailboxClientPath" --format text thread --message-id $messageId.
3. Keep the task bounded to one mailbox-native, role-owned update in this repo.
4. Run focused validation for the exact surface you changed.
5. Reply exactly once with:
   python "$mailboxClientPath" reply --delivery-file "$sandboxDeliveryPath" --idempotency-key "oncall-$deliveryId-reply" --payload-json '{...}'
6. Do not run ack, nack, or reply --ack-after. The supervisor will ack on exit code 0 and nack on non-zero.
7. If the task is too broad but you can explain why safely, send a deferred reply and still exit 0.
8. Exit non-zero only for transient failure where retry is desired.
"@
$workspaceContext = @"
Workspace root:
- use $workspaceRoot as the repo root for all reads, edits, and validation commands
- do not switch to a different checkout unless the mailbox task explicitly requires it
"@

Push-Location $workspaceRoot
try {
    $loginArgs = @($mailboxClientPath, "login", "--output", "token")
    $loginProjectId = [string]$configJson.project_id
    if ([string]::IsNullOrWhiteSpace($loginProjectId)) {
        throw "Runtime config is missing project_id."
    }
    $loginArgs += @("--project-id", $loginProjectId)
    if ($configJson.roles) {
        $joinedRoles = [string]::Join(",", @($configJson.roles))
        if (-not [string]::IsNullOrWhiteSpace($joinedRoles)) {
            $loginArgs += @("--roles", $joinedRoles)
        }
    }
    elseif ($configJson.role) {
        $loginArgs += @("--role", [string]$configJson.role)
    }
    if ($configJson.session) {
        $loginArgs += @("--session", [string]$configJson.session)
    }
    if ($configJson.agent_name) {
        $loginArgs += @("--agent-name", [string]$configJson.agent_name)
    }
    if ($configJson.local_part) {
        $loginArgs += @("--local-part", [string]$configJson.local_part)
    }
    if ($configJson.mailbox_type) {
        $loginArgs += @("--mailbox-type", [string]$configJson.mailbox_type)
    }
    $env:MAILBOX_SESSION_TOKEN = (python @loginArgs).Trim()
    Remove-Item Env:MAILBOX_TOKEN -ErrorAction SilentlyContinue

    $promptText = $workspaceContext + "`r`n`r`n" + (Get-Content $promptPath -Raw) + $oncallContext
    $promptText |
      codex exec -C $workspaceRoot --full-auto --skip-git-repo-check `
        -c model="gpt-5.4" `
        -c model_reasoning_effort="$resolvedEffort" `
        -c approval_policy="never" `
        -c sandbox_mode="workspace-write" `
        -o $lastMessagePath -
}
finally {
    Pop-Location
}
