@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0.."
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "PY_VER=3.11.9"
set "PY_DIR=%CD%\tools\python311"
set "PY_EXE=%PY_DIR%\python.exe"
set "PY_ZIP=%CD%\tools\python-%PY_VER%-embed-amd64.zip"
set "PY_URL=https://www.python.org/ftp/python/%PY_VER%/python-%PY_VER%-embed-amd64.zip"
set "GET_PIP=%PY_DIR%\get-pip.py"
set "GET_PIP_URL=https://bootstrap.pypa.io/get-pip.py"

if not exist "%PY_EXE%" (
  echo [niconico-watch-app] Downloading project-local Python %PY_VER%...
  if not exist "%CD%\tools" mkdir "%CD%\tools"
  if not exist "%PY_DIR%" mkdir "%PY_DIR%"
  powershell -NoProfile -ExecutionPolicy Bypass -Command "Invoke-WebRequest -Uri '%PY_URL%' -OutFile '%PY_ZIP%'"
  if errorlevel 1 goto :error
  powershell -NoProfile -ExecutionPolicy Bypass -Command "Expand-Archive -LiteralPath '%PY_ZIP%' -DestinationPath '%PY_DIR%' -Force"
  if errorlevel 1 goto :error
  del /q "%PY_ZIP%"
)

for %%F in ("%PY_DIR%\python*._pth") do powershell -NoProfile -ExecutionPolicy Bypass -Command "(Get-Content -LiteralPath '%%F') -replace '#import site','import site' | Set-Content -LiteralPath '%%F' -Encoding ascii"

"%PY_EXE%" -m pip --version >nul 2>nul
if errorlevel 1 (
  echo [niconico-watch-app] Installing pip...
  powershell -NoProfile -ExecutionPolicy Bypass -Command "Invoke-WebRequest -Uri '%GET_PIP_URL%' -OutFile '%GET_PIP%'"
  if errorlevel 1 goto :error
  "%PY_EXE%" "%GET_PIP%" --no-warn-script-location
  if errorlevel 1 goto :error
  del /q "%GET_PIP%"
)

echo [niconico-watch-app] Installing virtualenv...
"%PY_EXE%" -m pip install -U pip virtualenv --no-warn-script-location
if errorlevel 1 goto :error

if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" -c "import sys; raise SystemExit(0 if sys.version_info[:2] == (3,11) else 1)"
  if errorlevel 1 (
    echo [niconico-watch-app] Removing incompatible .venv...
    powershell -NoProfile -ExecutionPolicy Bypass -Command "Remove-Item -LiteralPath '%CD%\.venv' -Recurse -Force"
  )
)

if not exist ".venv\Scripts\python.exe" (
  echo [niconico-watch-app] Creating project-local Python 3.11 .venv...
  "%PY_EXE%" -m virtualenv ".venv"
  if errorlevel 1 goto :error
)

echo [niconico-watch-app] Installing requirements...
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto :error

echo [niconico-watch-app] .venv setup completed
exit /b 0

:error
echo [niconico-watch-app] .venv setup failed
exit /b 1
