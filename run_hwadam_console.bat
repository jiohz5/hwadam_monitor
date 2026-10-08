@echo off
chcp 65001 >nul
cd /d "%~dp0"
if exist "%USERPROFILE%\anaconda3\python.exe" (
    "%USERPROFILE%\anaconda3\python.exe" -X utf8 "%~dp0main.py"
) else (
    python -X utf8 "%~dp0main.py"
)
pause
