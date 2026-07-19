@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"

set "SETUP_LOG=%~dp0setup_log.txt"
set "FFMPEG_ROOT=%~dp0tools\ffmpeg"
set "FFMPEG_ZIP=%~dp0tools\ffmpeg-release-essentials.zip"
set "FFMPEG_URL=https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"

echo === niconico-watch-app setup %date% %time% === > "%SETUP_LOG%"

if not exist ".venv\Scripts\python.exe" (
  echo [1/3] Creating project-local .venv...
  call "%~dp0scripts\setup_venv.cmd"
  if errorlevel 1 goto :error
) else (
  echo [1/3] Project-local .venv already exists.
)

echo [2/4] Installing/updating packages...
"%~dp0.venv\Scripts\python.exe" -m pip install -U pip >> "%SETUP_LOG%" 2>&1
if errorlevel 1 goto :error
"%~dp0.venv\Scripts\python.exe" -m pip install -r "%~dp0requirements.txt" >> "%SETUP_LOG%" 2>&1
if errorlevel 1 goto :error

echo [3/4] Checking FFmpeg...
set "FFMPEG_EXE="
if exist "%FFMPEG_ROOT%" for /r "%FFMPEG_ROOT%" %%F in (ffmpeg.exe) do if not defined FFMPEG_EXE set "FFMPEG_EXE=%%F"
if not defined FFMPEG_EXE (
  if not exist "%~dp0tools" mkdir "%~dp0tools"
  echo Downloading FFmpeg...
  powershell -NoProfile -ExecutionPolicy Bypass -Command "Invoke-WebRequest -Uri '%FFMPEG_URL%' -OutFile '%FFMPEG_ZIP%'"
  if errorlevel 1 goto :error
  echo Extracting FFmpeg...
  if not exist "%FFMPEG_ROOT%" mkdir "%FFMPEG_ROOT%"
  powershell -NoProfile -ExecutionPolicy Bypass -Command "Expand-Archive -LiteralPath '%FFMPEG_ZIP%' -DestinationPath '%FFMPEG_ROOT%' -Force"
  if errorlevel 1 goto :error
  del /q "%FFMPEG_ZIP%"
)

echo [4/4] Setup complete.
echo ready>"%~dp0.setup_complete"
echo setup complete >> "%SETUP_LOG%"
if /I "%~1"=="--no-pause" exit /b 0
pause
exit /b 0

:error
if exist "%~dp0.setup_complete" del /q "%~dp0.setup_complete"
echo [ERROR] Setup failed. See setup_log.txt.
echo setup failed >> "%SETUP_LOG%"
if /I "%~1"=="--no-pause" exit /b 1
pause
exit /b 1
