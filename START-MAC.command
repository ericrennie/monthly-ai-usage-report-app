#!/bin/zsh
set -eu

app_dir="${0:A:h}"
if ! command -v python3 >/dev/null 2>&1; then
  echo "Python 3.11 or newer is required. Install it from https://www.python.org/downloads/"
  read -r "reply?Press Return to close."
  exit 1
fi
exec python3 "$app_dir/scripts/report_app.py"
