@echo off
cd /d "%~dp0"
if exist "%~dp0dist\TextLab.exe" (
    start "" "%~dp0dist\TextLab.exe"
    exit /b
)
where py >nul 2>nul
if not errorlevel 1 (
    py -3 run.py
) else (
    python run.py
)
if errorlevel 1 pause
