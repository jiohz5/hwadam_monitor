@echo off
chcp 65001 >nul
setlocal
set "HWADAM_IMPORT_TARGET=D:\지환\vscode\hwadam_codex"
powershell.exe -NoLogo -NoProfile -Command ^
 "$ErrorActionPreference='Stop'; try {" ^
 "$source=Join-Path $env:USERPROFILE 'Downloads\gui.py';" ^
 "if (-not (Test-Path -LiteralPath $source -PathType Leaf)) { throw '다운로드 폴더에 gui.py가 없습니다.' };" ^
 "if (-not (Test-Path -LiteralPath $env:HWADAM_IMPORT_TARGET -PathType Container)) { throw '작업 폴더를 찾을 수 없습니다.' };" ^
 "$launcher=Join-Path $env:HWADAM_IMPORT_TARGET 'run_hwadam.vbs';" ^
 "if (-not (Test-Path -LiteralPath $launcher -PathType Leaf)) { throw '작업 폴더에 run_hwadam.vbs가 없습니다.' };" ^
 "$destination=Join-Path $env:HWADAM_IMPORT_TARGET 'gui.py';" ^
 "Move-Item -LiteralPath $source -Destination $destination -Force;" ^
 "Write-Host ('이동 완료: '+$destination) -ForegroundColor Green;" ^
 "Start-Process -FilePath (Join-Path $env:SystemRoot 'System32\wscript.exe') -ArgumentList ('//Nologo '+[char]34+$launcher+[char]34) -WorkingDirectory $env:HWADAM_IMPORT_TARGET -WindowStyle Hidden;" ^
 "Write-Host 'run_hwadam.vbs 실행 요청 완료'; exit 0" ^
 "} catch { Write-Host $_.Exception.Message -ForegroundColor Red; exit 1 }"
set "HWADAM_IMPORT_RESULT=%ERRORLEVEL%"
if "%HWADAM_IMPORT_RESULT%"=="0" exit /b 0
echo.
pause
exit /b %HWADAM_IMPORT_RESULT%
