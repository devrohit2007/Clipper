#!/usr/bin/env sh
# Starts the Clipper web app. On Termux this also keeps the phone awake while it runs.
cd "$(dirname "$0")" || exit 1
command -v termux-wake-lock >/dev/null 2>&1 && termux-wake-lock
exec python server.py "$@"
