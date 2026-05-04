@echo off
setlocal
cd /d "%~dp0"
call "%~dp0run-weekly-call_sessions.bat"
if errorlevel 1 exit /b 1
call "%~dp0run-weekly-kakao_sessions.bat"
if errorlevel 1 exit /b 1
call "%~dp0run-weekly-viral_marketing_logs.bat"
if errorlevel 1 exit /b 1
