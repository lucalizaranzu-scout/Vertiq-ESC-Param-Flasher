#!/usr/bin/env bash
# Launch the ESC flasher GUI. Extra arguments are passed through,
# e.g. ./run.sh --positions M2
# Creates .venv on first run and reinstalls whenever requirements.txt changes.
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"

die() {
    echo "error: $*" >&2
    if [[ -n ${log:-} ]]; then
        notify-send "ESC Flasher failed to start" "$* (log: $log)" 2>/dev/null || true
    fi
    exit 1
}

# Started from the application menu (no terminal): keep output in a log file.
if [[ ! -t 2 ]]; then
    log="${XDG_CACHE_HOME:-$HOME/.cache}/esc-flasher.log"
    mkdir -p "$(dirname "$log")"
    exec >"$log" 2>&1
fi

if ! command -v python3 >/dev/null; then
    die "python3 is not installed (Debian/Ubuntu: sudo apt install python3)"
fi
if ! python3 -c 'import tkinter' 2>/dev/null; then
    die "Python's Tk support is missing (Debian/Ubuntu: sudo apt install python3-tk," \
        "Fedora: sudo dnf install python3-tkinter, Arch: sudo pacman -S tk)"
fi

stamp=.venv/.requirements.txt
if [[ ! -x .venv/bin/python ]] || ! cmp -s requirements.txt "$stamp"; then
    echo "Setting up Python environment..."
    if command -v uv >/dev/null; then
        [[ -x .venv/bin/python ]] || uv venv --quiet --python "$(command -v python3)" .venv
        uv pip install --quiet --python .venv/bin/python -r requirements.txt
    else
        if [[ ! -x .venv/bin/python ]]; then
            python3 -m venv .venv 2>/dev/null || {
                rm -rf .venv
                die "could not create a virtualenv (Debian/Ubuntu: sudo apt install python3-venv)"
            }
        fi
        .venv/bin/python -m pip install --quiet -r requirements.txt
    fi
    cp requirements.txt "$stamp"
fi

exec .venv/bin/python esc_flash/gui.py "$@"
