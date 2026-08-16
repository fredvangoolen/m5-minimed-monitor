#!/bin/bash
# Launcher for the Minimed PC Monitor desktop app.
cd "$(dirname "$0")" || exit 1
exec ./.venv/bin/python3 minimed-mon-pc.py
