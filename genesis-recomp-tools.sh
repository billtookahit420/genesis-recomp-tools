#!/bin/sh
# Launch GenesisRecomp Tools. On first run (fresh download), create the
# bundled virtual environment and install the app's Python deps.
# With no arguments this opens the GUI. The engine CLI is still
# available: ./genesis-recomp-tools.sh --cli toolbox|probe|run ...
# (the bare toolbox / probe / run subcommands pass through too).
cd "$(dirname "$0")" || exit 1
if [ ! -x .venv/bin/python ]; then
    echo "First run: setting up the Python environment..."
    python3 -m venv .venv || {
        echo "Could not create the environment. On Debian/Ubuntu run:"
        echo "  sudo apt install -y python3-venv"
        echo "then run ./genesis-recomp-tools.sh again."
        exit 1
    }
fi
if ! .venv/bin/python -c "import PySide6, PIL" 2>/dev/null; then
    echo "Installing Python dependencies..."
    .venv/bin/pip install -r requirements.txt || {
        echo "Dependency install failed - check your connection and re-run."
        exit 1
    }
fi
case "$1" in
    --cli)
        shift
        exec .venv/bin/python app/engine.py "$@"
        ;;
    toolbox|probe|run)
        exec .venv/bin/python app/engine.py "$@"
        ;;
    *)
        exec .venv/bin/python app/main.py "$@"
        ;;
esac
