@echo off
REM Thin repo-local launcher that delegates startup behavior to the shared codex-shared script.
REM Keep this file simple so the shared launcher remains the single source of truth.
setlocal
where pwsh.exe >nul 2>nul
if %ERRORLEVEL% EQU 0 (
    pwsh.exe -NoLogo -NoExit -ExecutionPolicy Bypass -File "C:\Users\JackOswald\OneDrive - ISOThrive Inc\codex-shared\Start-CodexRepo.ps1" -RepoPath "C:\Users\JackOswald\OneDrive - ISOThrive Inc\Personal\Flying\Weather\CODEX-Weather-Brief" %*
) else (
    powershell.exe -NoLogo -NoExit -ExecutionPolicy Bypass -File "C:\Users\JackOswald\OneDrive - ISOThrive Inc\codex-shared\Start-CodexRepo.ps1" -RepoPath "C:\Users\JackOswald\OneDrive - ISOThrive Inc\Personal\Flying\Weather\CODEX-Weather-Brief" %*
)
