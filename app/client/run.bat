@echo off
REM Start the SVE client (local web UI + backend).
REM
REM ASCII only: cmd.exe reads .bat as the OEM codepage (GBK here), so non-ASCII
REM comments get mangled into bogus commands.
REM
REM Usage:  app\client\run.bat            (then open http://127.0.0.1:8801)
setlocal
cd /d "%~dp0..\.."
set SVE_CLIENT_PORT=8801
"%~dp0..\..\vendor\venv-client\Scripts\python.exe" -u "%~dp0server.py" %*
