@echo off
rem Run mitmdump from this project's private venv without relying on the
rem *.exe shim inside the venv (those shims embed absolute paths and break if
rem the venv folder is moved).
rem
rem Usage:
rem   scripts\capture\mitmdump.cmd --set confdir="capture\confdir" -p 8080 -s scripts\capture\sve_capture.py -w capture\capture.flow
setlocal
set "HERE=%~dp0"
set "VENV_PY=%HERE%..\..\vendor\venv314\Scripts\python.exe"
if not exist "%VENV_PY%" (
  echo [mitmdump.cmd] venv python not found: "%VENV_PY%" 1>&2
  exit /b 1
)
"%VENV_PY%" -c "import sys; from mitmproxy.tools import main; sys.argv=['mitmdump']+sys.argv[1:]; main.mitmdump()" %*
exit /b %ERRORLEVEL%
