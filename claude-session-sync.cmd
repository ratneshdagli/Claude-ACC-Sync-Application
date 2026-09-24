@echo off
rem claude-session-sync launcher (CLI). Uses the Python launcher if present, else python on PATH.
setlocal
set "PYTHONPATH=%~dp0;%PYTHONPATH%"
where py >nul 2>nul
if %ERRORLEVEL%==0 (py -3 -X utf8 -m claude_session_sync %*) else (python -X utf8 -m claude_session_sync %*)
exit /b %ERRORLEVEL%
