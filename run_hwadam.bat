@echo off
chcp 65001 >nul
cd /d "%~dp0"
if exist "%USERPROFILE%\anaconda3\pythonw.exe" (
    start "" "%USERPROFILE%\anaconda3\pythonw.exe" "%~dp0main.py"
    exit /b
)
where pythonw >nul 2>nul
if not errorlevel 1 (
    start "" pythonw "%~dp0main.py"
    exit /b
)
where py >nul 2>nul
if not errorlevel 1 (
    py -3 "%~dp0main.py"
    exit /b
)
echo Python 3.10 이상이 필요합니다.
pause
