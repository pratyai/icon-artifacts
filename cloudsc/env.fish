# This script should be sourced in fish: source env.fish

set VENV_DIR ".venv"

if not test -d "$VENV_DIR"
    echo "Error: Virtual environment directory '$VENV_DIR' not found."
    echo "Please run: python3 -m venv .venv"
    exit 1
end

source "$VENV_DIR/bin/activate.fish"
set -gx PYTHONPATH $PYTHONPATH (pwd)

echo "Environment activated (fish)."
echo "Python location: "(which python)
echo "DaCe version: "(python -c 'import dace; print(dace.__version__)' 2>/dev/null; or echo 'Not installed')
