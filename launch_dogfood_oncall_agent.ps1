param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("operator")]
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
}

$selected = $profileMap[$Role]
$resolvedEffort = if ($ReasoningEffort) { $ReasoningEffort } else { [string]$selected.DefaultEffort }

$tokenPath = Join-Path $runtimePath "harness.token"
$summaryPath = Join-Path $runtimePath "bootstrap_summary.json"
$configPath = Join-Path $runtimePath ([string]$selected.ConfigFile)
$promptPath = Join-Path $root ([string]$selected.PromptFile)
$lastMessagePath = Join-Path $runtimePath "$Role-oncall-last-message.txt"
$deliveryPath = Join-Path $runtimePath "$Role-current-delivery.json"
$sandboxRuntimeDir = Join-Path $root (Join-Path ".tmp_dogfood_live" "$Role-oncall")
$sandboxConfigPath = Join-Path $sandboxRuntimeDir ([string]$selected.ConfigFile)
$sandboxDeliveryPath = Join-Path $sandboxRuntimeDir "$Role-current-delivery.json"
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
$env:MAILBOX_TOKEN = (Get-Content $tokenPath -Raw).Trim()
$env:MAILBOX_TIMEOUT_SECONDS = "15"
$env:MAILBOX_HARNESS_ID = [string]$summary.harness_id
$env:MAILBOX_PROJECT_ID = [string]$summary.project_id
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
1. The supervisor already claimed this delivery. Do not run python .\client.py claim.
2. Read the thread with python .\client.py --format text thread --message-id $messageId.
3. Keep the task bounded to one mailbox-native operator update in this repo.
4. Run focused validation for the exact surface you changed.
5. Reply exactly once with:
   python .\client.py reply --delivery-file "$sandboxDeliveryPath" --idempotency-key "oncall-$deliveryId-reply" --payload-json '{...}'
6. Do not run ack, nack, or reply --ack-after. The supervisor will ack on exit code 0 and nack on non-zero.
7. If the task is too broad but you can explain why safely, send a deferred reply and still exit 0.
8. Exit non-zero only for transient failure where retry is desired.
"@

Push-Location $root
try {
    $loginArgs = @(".\client.py", "login", "--output", "token")
    $loginArgs += [string[]]$selected.LoginArgs
    $env:MAILBOX_SESSION_TOKEN = (python @loginArgs).Trim()
    Remove-Item Env:MAILBOX_TOKEN -ErrorAction SilentlyContinue

    $promptText = (Get-Content $promptPath -Raw) + $oncallContext
    $promptText |
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
