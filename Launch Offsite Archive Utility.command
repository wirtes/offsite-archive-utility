#!/bin/zsh
set -u

APP_DIR="/Users/wirtes/Code/Codex Projects/offsite-archive-utility"
APP_URL="http://127.0.0.1:8585"

echo "Starting Offsite Archive Utility..."
echo "App folder: ${APP_DIR}"
echo "Web UI: ${APP_URL}"
echo

if [ ! -d "${APP_DIR}" ]; then
  echo "Could not find the app folder:"
  echo "${APP_DIR}"
  echo
  echo "If the repo moved, edit this launcher and update APP_DIR."
  echo "Press Return to close this window."
  read -r
  exit 1
fi

cd "${APP_DIR}" || exit 1

echo "Leave this Terminal window open while you use the backup website."
echo "Close this window or press Ctrl-C here to stop the website."
echo

(sleep 1; open "${APP_URL}" >/dev/null 2>&1) &

python3 app.py

echo
echo "Offsite Archive Utility stopped."
echo "Press Return to close this window."
read -r
