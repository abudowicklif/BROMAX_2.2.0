@echo off
setlocal
cd /d "%~dp0"
if not exist .venv\Scripts\activate.bat (
  echo Run INSTALL_AND_RUN.bat first to create the environment.
  pause
  exit /b 1
)
call .venv\Scripts\activate.bat
pytest -q
pause
endlocal
