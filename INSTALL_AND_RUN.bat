@echo off
setlocal
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
  py -3 -c "import sys; assert sys.version_info >= (3,11), 'Python 3.11 or newer required'"
  if errorlevel 1 goto error
  py -3 -m venv .venv
  if errorlevel 1 goto error
)
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
if errorlevel 1 goto error
pip install -e ".[test]"
if errorlevel 1 goto error
if not exist .env copy .env.example .env
start "BROMAX Server" cmd /k python -m uvicorn app:app --host 127.0.0.1 --port 8765
timeout /t 3 /nobreak >nul
start "BROMAX Floating Panel" cmd /k python -m bromax.desktop
start "" http://127.0.0.1:8765/dashboard
goto end
:error
echo BROMAX setup failed. Review the error above and check Python 3.11+ is installed.
pause
:end
endlocal
