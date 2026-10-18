@echo off
rem GPS Scope on Windows: monitor the GPS on a Betaflight / INAV flight controller through USB passthrough.
rem   start_windows.bat                 FC found automatically
rem   start_windows.bat --fc COM5       name the FC's port
rem   start_windows.bat --port COM7     a GPS on a USB-UART adapter instead (no FC)
rem Close Betaflight Configurator first: only one program can use the COM port.
cd /d "%~dp0"
set PYTHONUTF8=1
if not exist venv\Scripts\python.exe (
  echo Setting up Python environment, first run only...
  py -3 -m venv venv 2>nul || python -m venv venv
  if not exist venv\Scripts\python.exe (
    echo Python 3 not found. Install it from https://www.python.org/downloads/ and tick "Add python.exe to PATH".
    pause
    exit /b 1
  )
  venv\Scripts\python -m pip install -q --upgrade pip
)
venv\Scripts\python -m pip install -q -r requirements.txt
set ARGS=%*
if "%ARGS%"=="" set ARGS=--fc auto
start "" /b cmd /c "timeout /t 4 >nul & start http://127.0.0.1:8080"
venv\Scripts\python gpsscope.py %ARGS% --http 127.0.0.1:8080
pause
