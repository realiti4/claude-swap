<#
.SYNOPSIS
  Removes the scheduled task created by install-autostart.ps1, so cswap auto
  no longer starts automatically at logon.

.PARAMETER TaskName
  Name of the scheduled task to remove (default: "Claude Swap Auto").

.EXAMPLE
  .\uninstall-autostart.ps1
#>

[CmdletBinding()]
param(
    [string]$TaskName = "Claude Swap Auto"
)

$ErrorActionPreference = "Stop"

$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if (-not $task) {
    Write-Host "No scheduled task named '$TaskName' found - nothing to remove."
    exit 0
}

if ($task.State -eq "Running") {
    Stop-ScheduledTask -TaskName $TaskName
}
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false

Write-Host "Removed scheduled task '$TaskName'. cswap auto will no longer start automatically at logon."
