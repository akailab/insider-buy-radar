@echo off
REM Double-click to start Insider Buy Radar on Windows.
cd /d "%~dp0"
set PY=python
where py >nul 2>nul && set PY=py -3
%PY% --version >nul 2>nul || (echo Python 3 is not installed. Get it from https://www.python.org/downloads/ and tick "Add python.exe to PATH". & pause & exit /b 1)
if not exist .venv\Scripts\python.exe (
  echo First run: setting up...
  %PY% -m venv .venv >nul 2>nul && .venv\Scripts\python -m pip install --quiet --disable-pip-version-check certifi pypdf >nul 2>nul
)
if exist .venv\Scripts\python.exe (.venv\Scripts\python -c "import pypdf" >nul 2>nul || .venv\Scripts\python -m pip install --quiet --disable-pip-version-check pypdf >nul 2>nul)
if exist .venv\Scripts\python.exe (.venv\Scripts\python insider_radar.py) else (%PY% insider_radar.py)
pause
