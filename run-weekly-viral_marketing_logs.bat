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

set VIRAL_BASE_DIR=!VM_VIRAL_BASE_DIR!
set EXTRACT_SCRIPT=!VM_EXTRACT_SCRIPT!
set OUTPUT_ROOT=!VM_OUTPUT_ROOT!
set LOG_DIR=!VM_LOG_DIR!
set CHECKPOINT_DIR=!VM_CHECKPOINT_DIR!
set BATCH_SIZE=!VM_BATCH_SIZE!
set SLEEP_SEC=!VM_SLEEP_SEC!
set PYTHON_BIN=!VM_PYTHON_BIN!

"!PYTHON_BIN!" "!VM_RUN_SCRIPT!"
