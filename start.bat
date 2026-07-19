@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"

set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "FFMPEG_EXE="
if exist "%~dp0tools\ffmpeg" for /r "%~dp0tools\ffmpeg" %%F in (ffmpeg.exe) do if not defined FFMPEG_EXE set "FFMPEG_EXE=%%F"

if not exist ".setup_complete" goto :setup
if not exist ".venv\Scripts\python.exe" goto :setup
if not defined FFMPEG_EXE goto :setup
goto :launch

:setup
echo Required local runtime is missing. Running setup...
call "%~dp0setup.bat" --no-pause
if errorlevel 1 (
  echo [ERROR] Setup failed.
  pause
  exit /b 1
)
set "FFMPEG_EXE="
for /r "%~dp0tools\ffmpeg" %%F in (ffmpeg.exe) do if not defined FFMPEG_EXE set "FFMPEG_EXE=%%F"

:launch
for %%D in ("%FFMPEG_EXE%") do set "PATH=%%~dpD;%PATH%"
call "%~dp0scripts\start_gui.cmd"
exit /b %ERRORLEVEL%
