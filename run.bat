@echo off
setlocal EnableExtensions
cd /d "%~dp0"

REM run.bat              -> opens the desktop window
REM run.bat ABC-123 ...  -> command line (same options as jcl.py)

set "PYTHON="
for %%P in (
  "%LocalAppData%\Programs\Python\Python313\python.exe"
  "%LocalAppData%\Programs\Python\Python312\python.exe"
  "%LocalAppData%\Programs\Python\Python311\python.exe"
  "%LocalAppData%\Programs\Python\Python310\python.exe"
) do (
  if not defined PYTHON if exist %%~P set "PYTHON=%%~P"
)
if not defined PYTHON set "PYTHON=python"

"%PYTHON%" -c "import requests" >nul 2>&1
if errorlevel 1 "%PYTHON%" -m pip install -r "%~dp0requirements.txt"

if not "%~1"=="" (
  "%PYTHON%" "%~dp0jcl.py" %*
  exit /b %ERRORLEVEL%
)

REM No arguments: open the window without a console (pythonw sits next to python).
for %%I in ("%PYTHON%") do set "PYTHONW=%%~dpIpythonw.exe"
if exist "%PYTHONW%" (
  start "" "%PYTHONW%" "%~dp0launch.pyw"
  exit /b 0
)
"%PYTHON%" "%~dp0launch.pyw"
if errorlevel 1 (
  echo.
  echo If the window closed immediately, see launch_error.log
  pause
)
