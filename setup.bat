@echo off
rem aside-long setup - safe to run again.
rem NOTE: .bat files must stay ASCII + CRLF.
setlocal
cd /d "%~dp0"
chcp 65001 >nul
set PYTHONUTF8=1

echo [1/4] Python venv (.venv)
set "PYEXE=python"
where py >nul 2>nul && set "PYEXE=py -3"
if not exist ".venv\Scripts\python.exe" (
  %PYEXE% -m venv .venv
  if errorlevel 1 goto :fail
)

echo [2/4] Python packages
".venv\Scripts\python" -m pip install -q --upgrade pip
".venv\Scripts\python" -m pip install -q -r requirements.txt
if errorlevel 1 goto :fail

echo [3/4] SuperTonic3 voice model (reuses aside-youtube-claude copy if present)
".venv\Scripts\python" -m aside_long tts-setup
if errorlevel 1 goto :fail

echo [4/4] Doctor (Claude, Codex, SuperTonic3, ffmpeg)
".venv\Scripts\python" -m aside_long doctor

echo.
echo Setup finished.
echo Next: 1) claude auth login   (Claude subscription - rewrite, script, scoring)
echo       2) codex login         (ChatGPT account - images)
echo       3) run.bat             (opens the dashboard)
pause
exit /b 0

:fail
echo.
echo SETUP FAILED - read the messages above.
pause
exit /b 1
