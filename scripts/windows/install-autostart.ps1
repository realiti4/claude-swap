<#
.SYNOPSIS
  Registers a Windows Scheduled Task that keeps `cswap auto` running
  permanently in the background — starts at logon, restarts itself if it
  crashes, no terminal window required.

.PARAMETER NoNotify
  Suppress desktop toast notifications from the background task.

.PARAMETER TaskName
  Name of the scheduled task (default: "Claude Swap Auto"). Override only
  if you need to run more than one instance under different names.

.EXAMPLE
  .\install-autostart.ps1
  .\install-autostart.ps1 -NoNotify
#>

[CmdletBinding()]
param(
    [switch]$NoNotify,
    [string]$TaskName = "Claude Swap Auto"
)

$ErrorActionPreference = "Stop"

$cswap = Get-Command cswap -ErrorAction SilentlyContinue
if (-not $cswap) {
    Write-Error "cswap not found on PATH. Install it first (pipx install claude-swap or pip install -e . from this repo), then restart this terminal and re-run this script."
    exit 1
}

$notifyFlag = if ($NoNotify) { " --no-notify" } else { "" }
$command = "cswap auto$notifyFlag"
$argument = "-NoLogo -NoProfile -WindowStyle Hidden -Command `"$command`""

$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $argument
$trigger = New-ScheduledTaskTrigger -AtLogOn
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero)

# -ExecutionTimeLimit Zero is required: Task Scheduler's default kills any
# task still running after 72 hours, which would silently stop a loop meant
# to run forever.

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Principal $principal -Settings $settings -Force | Out-Null

Start-ScheduledTask -TaskName $TaskName

Write-Host ""
Write-Host "Installed and started scheduled task '$TaskName'."
Write-Host "cswap auto now starts automatically every time you log in - no terminal needed."
Write-Host ""
Write-Host "Check it's running:  Get-ScheduledTask -TaskName '$TaskName' | Get-ScheduledTaskInfo"
Write-Host "Stop it for now:     Stop-ScheduledTask -TaskName '$TaskName'"
Write-Host "Remove it entirely:  .\uninstall-autostart.ps1"
