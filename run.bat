@echo off
REM Windows launcher (equivalent of run.sh for Linux/WSL).
REM Uses the system Python; ensures duckdb is installed, then runs the daily job.
setlocal

REM Show Unicode (arrows, mid-dots) correctly in the console.
chcp 65001 >nul
set PYTHONUTF8=1

set "SCRIPT_DIR=%~dp0"

REM Ensure duckdb is available for the active Python.
python -c "import duckdb" 2>nul
if errorlevel 1 (
    echo Installing duckdb...
    python -m pip install duckdb --quiet --disable-pip-version-check
)

python "%SCRIPT_DIR%run_all.py" %*
endlocal
