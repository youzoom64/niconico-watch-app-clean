@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0.."
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "PY_VER=3.11.9"
set "LOCAL_PYTHON=%CD%\tools\python311\python.exe"
set "PY_INSTALLER=%CD%\tools\python-%PY_VER%-amd64.exe"
set "PY_URL=https://www.python.org/ftp/python/%PY_VER%/python-%PY_VER%-amd64.exe"
set "PYTHON_BASE="

if exist "%LOCAL_PYTHON%" set "PYTHON_BASE=%LOCAL_PYTHON%"
if not defined PYTHON_BASE where py >nul 2>nul && py -3.11 -c "import sys; assert sys.version_info[:2] == (3,11)" >nul 2>nul && set "PYTHON_BASE=py -3.11"

if not defined PYTHON_BASE (
  echo [niconico-watch-app] Python 3.11 not found. Installing a project-local runtime...
  if not exist "%CD%\tools" mkdir "%CD%\tools"
  powershell -NoProfile -ExecutionPolicy Bypass -Command "Invoke-WebRequest -Uri '%PY_URL%' -OutFile '%PY_INSTALLER%'"
  if errorlevel 1 goto :error
  "%PY_INSTALLER%" /quiet InstallAllUsers=0 TargetDir="%CD%\tools\python311" Include_pip=1 Include_launcher=0 Include_test=0 Include_doc=0 Include_tcltk=0 Shortcuts=0 PrependPath=0
  if errorlevel 1 goto :error
  del /q "%PY_INSTALLER%"
  if not exist "%LOCAL_PYTHON%" goto :error
  set "PYTHON_BASE=%LOCAL_PYTHON%"
)

if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" -c "import sys; raise SystemExit(0 if sys.version_info[:2] == (3,11) else 1)"
  if errorlevel 1 (
    echo [niconico-watch-app] Removing incompatible .venv...
    powershell -NoProfile -ExecutionPolicy Bypass -Command "Remove-Item -LiteralPath '%CD%\.venv' -Recurse -Force"
  )
)

if not exist ".venv\Scripts\python.exe" (
  echo [niconico-watch-app] creating Python 3.11 .venv
  %PYTHON_BASE% -m venv .venv
  if errorlevel 1 goto :error
)

echo [niconico-watch-app] upgrading pip
".venv\Scripts\python.exe" -m pip install -U pip
if errorlevel 1 goto :error

echo [niconico-watch-app] installing requirements
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto :error

echo [niconico-watch-app] .venv setup completed
exit /b 0

:error
echo [niconico-watch-app] .venv setup failed
exit /b 1
