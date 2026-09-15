@echo off
REM Double-click this file to restart the KariKariAIOps scheduled bot task.
REM It will ask for administrator permission once (click "Yes").
powershell -NoProfile -Command "Start-Process powershell -ArgumentList '-NoProfile -ExecutionPolicy Bypass -Command Stop-ScheduledTask -TaskName KariKariAIOps; Start-Sleep -Seconds 3; Start-ScheduledTask -TaskName KariKariAIOps; Write-Host DONE - bot restarted.; Start-Sleep -Seconds 5' -Verb RunAs"
