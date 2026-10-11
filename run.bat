@echo off
rem aside-long launcher (long-form 16:9 video console).
rem   run.bat               open the dashboard
rem   run.bat all --job X   any CLI command: python -m aside_long ...
rem NOTE: .bat files must stay ASCII + CRLF.
setlocal
cd /d "%~dp0"
chcp 65001 >nul
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
if not exist ".venv\Scripts\python.exe" (
  echo Run setup.bat first.
  pause
  exit /b 1
)
if "%~1"=="" (
  title aside-long - keep this window open while the dashboard is in use
  ".venv\Scripts\python" -m aside_long ui
) else (
  ".venv\Scripts\python" -m aside_long %*
)
