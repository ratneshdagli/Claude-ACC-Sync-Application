@echo off
rem Opens the Claude Session Sync window (Edge/Chrome app window on a local, token-protected port).
setlocal
set "PYTHONPATH=%~dp0;%PYTHONPATH%"
where pyw >nul 2>nul
if %ERRORLEVEL%==0 (start "" pyw -3 -X utf8 -m claude_session_sync gui) else (start "" python -X utf8 -m claude_session_sync gui)
