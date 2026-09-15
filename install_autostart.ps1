#Requires -RunAsAdministrator
# Sets up autostart-at-boot for the KariKari AI Ops Discord bot via Task Scheduler.
# Runs as SYSTEM, no desktop login required, auto-restarts on crash.
#
# Usage: open PowerShell "as Administrator" and run:
#   cd "C:\Kali Kali Server\kari-kari-ai-ops"
#   .\install_autostart.ps1

$ErrorActionPreference = "Stop"

$taskName   = "KariKariAIOps"
$projectDir = "C:\Kali Kali Server\kari-kari-ai-ops"
$pythonw    = Join-Path $projectDir ".venv\Scripts\pythonw.exe"

if (-not (Test-Path $pythonw)) {
    Write-Host "ERROR: not found: $pythonw" -ForegroundColor Red
    Write-Host "Make sure the virtual environment (.venv) has been created first." -ForegroundColor Red
    exit 1
}

$action    = New-ScheduledTaskAction -Execute $pythonw -Argument "bot.py" -WorkingDirectory $projectDir
$trigger   = New-ScheduledTaskTrigger -AtStartup
$principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
$settings  = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero)

Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
    -Principal $principal -Settings $settings `
    -Description "KariKari AI Ops Discord Bot - autostart at boot" -Force

Write-Host ""
Write-Host "DONE: scheduled task '$taskName' created." -ForegroundColor Green
Write-Host "It will start automatically at every boot, running in the background as SYSTEM (no login needed)." -ForegroundColor Green
Write-Host ""
Write-Host "Useful commands:"
Write-Host "  Start now:     Start-ScheduledTask -TaskName '$taskName'"
Write-Host "  Check status:  Get-ScheduledTask -TaskName '$taskName' | Get-ScheduledTaskInfo"
Write-Host "  Stop:          Stop-ScheduledTask -TaskName '$taskName'"
Write-Host "  Remove:        Unregister-ScheduledTask -TaskName '$taskName' -Confirm:`$false"
