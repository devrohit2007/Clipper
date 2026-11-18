#!/usr/bin/env sh
# Optional: start Clipper when the phone boots. Needs the Termux:Boot app.
#   mkdir -p ~/.termux/boot && cp termux-boot/start-clipper.sh ~/.termux/boot/
# Change the folder below if you unzipped Clipper somewhere else.
CLIPPER_DIR="$HOME/clipper"
cd "$CLIPPER_DIR" || exit 1
mkdir -p data
nohup sh start.sh >> data/boot.log 2>&1 &
