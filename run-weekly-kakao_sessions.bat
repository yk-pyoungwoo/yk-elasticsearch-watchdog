@echo off
setlocal EnableDelayedExpansion

set "ROOT=%~dp0"
if "!ROOT:~-1!"=="\" set "ROOT=!ROOT:~0,-1!"

set "ENVTEMP=%TEMP%\elasticsearch-watchdog-env-%RANDOM%.bat"
python "!ROOT!\env\bootstrap_dotenv.py" "!ROOT!\.env" "!ENVTEMP!"
if errorlevel 1 (
  echo [.env] bootstrap failed. Copy .env.example to .env and ensure python is on PATH.
  exit /b 1
)
call "!ENVTEMP!"
del "!ENVTEMP!" >nul 2>&1

set CALL_REPORT_BASE_DIR=!KS_CALL_REPORT_BASE_DIR!
set EXTRACT_SCRIPT=!KS_EXTRACT_SCRIPT!
set OUTPUT_ROOT=!KS_OUTPUT_ROOT!
set LOG_DIR=!KS_LOG_DIR!
set PYTHON_BIN=!KS_PYTHON_BIN!

"!PYTHON_BIN!" "!KS_RUN_SCRIPT!"
