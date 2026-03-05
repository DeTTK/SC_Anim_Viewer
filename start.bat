@echo off
setlocal
cd /d "%~dp0"
py -m pip install -r requirements.txt
if errorlevel 1 goto :deps_failed
py "stalcraft_anim_preview_desktop.py"
goto :eof

:deps_failed
echo Failed to install Python dependencies from requirements.txt
pause
exit /b 1
