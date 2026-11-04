@echo off
rem Launch GenesisRecomp Tools. On first run (fresh download), create
rem the bundled virtual environment and install the app's Python
rem dependencies. With no arguments this opens the GUI. The engine
rem CLI is still available: genesis-recomp-tools.bat --cli toolbox
rem (the bare toolbox / probe / run subcommands pass through too).
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo First run: setting up the Python environment...
    python -m venv .venv || (
        echo Could not create the environment. Install Python 3.10+
        echo from python.org, then run this file again.
        pause
        exit /b 1
    )
)
.venv\Scripts\python.exe -c "import PySide6, PIL" 2>nul
if errorlevel 1 (
    echo Installing Python dependencies...
    .venv\Scripts\python.exe -m pip install -r requirements.txt || (
        echo Dependency install failed - check your connection and re-run.
        pause
        exit /b 1
    )
)
if "%~1"=="--cli" (
    shift
    .venv\Scripts\python.exe app\engine.py %2 %3 %4 %5 %6 %7 %8 %9
    exit /b %errorlevel%
)
if "%~1"=="toolbox" goto cli
if "%~1"=="probe" goto cli
if "%~1"=="run" goto cli
.venv\Scripts\python.exe app\main.py %*
exit /b %errorlevel%
:cli
.venv\Scripts\python.exe app\engine.py %*
exit /b %errorlevel%
