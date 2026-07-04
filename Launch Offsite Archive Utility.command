#!/bin/zsh
set -u

APP_URL="http://127.0.0.1:8585"
CONFIG_DIR="${HOME}/Library/Application Support/Offsite Archive Utility Launcher"
CONFIG_FILE="${CONFIG_DIR}/app_dir"

SCRIPT_PATH="${(%):-%x}"
SCRIPT_DIR="$(cd "$(dirname "${SCRIPT_PATH}")" && pwd)"

prompt_for_app_dir() {
  local selected
  selected=$(osascript <<'APPLESCRIPT'
try
  set chosenFolder to choose folder with prompt "Choose the offsite-archive-utility repo folder:"
  POSIX path of chosenFolder
on error
  return ""
end try
APPLESCRIPT
)
  selected="${selected%/}"
  if [ -z "${selected}" ]; then
    echo "No folder selected."
    exit 1
  fi
  if [ ! -f "${selected}/app.py" ]; then
    echo "That folder does not contain app.py:"
    echo "${selected}"
    exit 1
  fi
  mkdir -p "${CONFIG_DIR}"
  printf '%s\n' "${selected}" > "${CONFIG_FILE}"
  echo "${selected}"
}

APP_DIR=""
if [ -f "${SCRIPT_DIR}/app.py" ]; then
  APP_DIR="${SCRIPT_DIR}"
elif [ -f "${CONFIG_FILE}" ]; then
  APP_DIR="$(cat "${CONFIG_FILE}")"
fi

if [ -z "${APP_DIR}" ] || [ ! -f "${APP_DIR}/app.py" ]; then
  APP_DIR="$(prompt_for_app_dir)"
fi

echo "Starting Offsite Archive Utility..."
echo "App folder: ${APP_DIR}"
echo "Web UI: ${APP_URL}"
echo

if [ ! -d "${APP_DIR}" ]; then
  echo "Could not find the app folder:"
  echo "${APP_DIR}"
  echo
  echo "Delete ${CONFIG_FILE} and launch again to choose a new folder."
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
