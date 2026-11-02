@echo off
where pwsh.exe >nul 2>nul
if errorlevel 1 (
  echo PowerShell 7 is required and free. Install it from:
  echo https://learn.microsoft.com/powershell/scripting/install/installing-powershell-on-windows
  echo Or use: winget install --id Microsoft.PowerShell --source winget
  echo Then run install.cmd again. No global execution-policy change is needed.
  pause
  exit /b 1
)
pwsh.exe -NoProfile -File "%~dp0install.ps1" -RegisterProfile %*
if errorlevel 1 pause
