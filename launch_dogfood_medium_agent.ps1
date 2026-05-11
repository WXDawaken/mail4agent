param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("planner", "reviewer", "operator")]
    [string]$Role,
    [string]$RuntimeDir = ".tmp_dogfood"
)

$launcherPath = Join-Path (Split-Path -Parent $MyInvocation.MyCommand.Path) "launch_dogfood_agent.ps1"
powershell -ExecutionPolicy Bypass -File $launcherPath -Role $Role -RuntimeDir $RuntimeDir
