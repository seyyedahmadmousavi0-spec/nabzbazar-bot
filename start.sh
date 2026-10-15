#!/usr/bin/env bash
set -e

echo "Starting MarketIntel..."

# Ensure werkzeug and requirements exist in python3 environment
if ! python3 -c "import werkzeug" >/dev/null 2>&1; then
    echo "Werkzeug not found in python3. Installing requirements..."
    python3 -m pip install --break-system-packages -r requirements.txt 2>/dev/null || python3 -m pip install -r requirements.txt 2>/dev/null || pip install -r requirements.txt 2>/dev/null || true
fi

if command -v python3 >/dev/null 2>&1; then
    exec python3 -m marketintel run-all --interval 20
elif command -v python >/dev/null 2>&1; then
    exec python -m marketintel run-all --interval 20
else
    echo "ERROR: Neither python3 nor python was found in PATH!" >&2
    exit 127
fi
