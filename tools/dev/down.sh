#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./env.sh
source "${SCRIPT_DIR}/env.sh"

usage() {
  cat <<'USAGE'
Usage:
  tools/dev/down.sh [--purge --yes] [--help]

Options:
  --purge  Remove the container and named volume after stopping.
  --yes    Required with --purge to guard destructive removal.
  --help   Show this help.
USAGE
}

require_cmd() {
  local cmd="$1"
  if ! command -v "${cmd}" >/dev/null 2>&1; then
    echo "[ERROR] Missing dependency: ${cmd}" >&2
    echo "[ERROR] Install '${cmd}' and re-run tools/dev/down.sh." >&2
    exit 1
  fi
}

require_cmd docker

purge=0
yes=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --purge)
      purge=1
      ;;
    --yes)
      yes=1
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "[ERROR] Unknown option: $1" >&2
      usage
      exit 1
      ;;
  esac
  shift
done

if [[ "${purge}" -eq 1 && "${yes}" -ne 1 ]]; then
  echo "[ERROR] Refusing purge without explicit confirmation." >&2
  echo "[ERROR] Re-run with: tools/dev/down.sh --purge --yes" >&2
  exit 1
fi

if docker ps -a --format '{{.Names}}' | grep -Fxq "${EPILEPSIAE_DB_CONTAINER}"; then
  status="$(docker inspect -f '{{.State.Status}}' "${EPILEPSIAE_DB_CONTAINER}")"
  if [[ "${status}" == "running" ]]; then
    echo "[INFO] Stopping container '${EPILEPSIAE_DB_CONTAINER}'."
    docker stop "${EPILEPSIAE_DB_CONTAINER}" >/dev/null
  else
    echo "[INFO] Container '${EPILEPSIAE_DB_CONTAINER}' already stopped (status=${status})."
  fi
else
  echo "[INFO] Container '${EPILEPSIAE_DB_CONTAINER}' not found."
fi

if [[ "${purge}" -eq 1 ]]; then
  if docker ps -a --format '{{.Names}}' | grep -Fxq "${EPILEPSIAE_DB_CONTAINER}"; then
    echo "[INFO] Removing container '${EPILEPSIAE_DB_CONTAINER}'."
    docker rm "${EPILEPSIAE_DB_CONTAINER}" >/dev/null
  fi

  if docker volume inspect "${EPILEPSIAE_DB_VOLUME}" >/dev/null 2>&1; then
    echo "[INFO] Removing volume '${EPILEPSIAE_DB_VOLUME}'."
    docker volume rm "${EPILEPSIAE_DB_VOLUME}" >/dev/null
  else
    echo "[INFO] Volume '${EPILEPSIAE_DB_VOLUME}' not found."
  fi
fi

echo "[OK] tools/dev/down.sh completed."
