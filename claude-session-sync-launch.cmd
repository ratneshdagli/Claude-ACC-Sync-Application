@echo off
rem Syncs your accounts (only possible while Claude Desktop is closed), then starts Claude Desktop.
setlocal
set "PYTHONPATH=%~dp0;%PYTHONPATH%"
where py >nul 2>nul
if %ERRORLEVEL%==0 (py -3 -X utf8 -m claude_session_sync launch) else (python -X utf8 -m claude_session_sync launch)
timeout /t 4 >nul
