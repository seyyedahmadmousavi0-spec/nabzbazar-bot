#!/usr/bin/env bash
set -e

echo "Starting MarketIntel..."
if command -v python3 >/dev/null 2>&1; then
    exec python3 -m marketintel run-all --interval 20
elif command -v python >/dev/null 2>&1; then
    exec python -m marketintel run-all --interval 20
else
    echo "ERROR: Neither python3 nor python was found in PATH!" >&2
    exit 127
fi

