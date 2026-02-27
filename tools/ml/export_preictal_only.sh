#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage:
  tools/ml/export_preictal_only.sh PAT_ID OUT WINDOW_SECONDS STRIDE_SECONDS LAYOUT [MAX_WINDOWS]

Example:
  tools/ml/export_preictal_only.sh 548 ./exports/pat_548_preictal.parquet 60 10 CTH_flat 200
USAGE
}

if [[ $# -lt 5 || $# -gt 6 ]]; then
  usage
  exit 1
fi

PAT_ID="$1"
OUT="$2"
WINDOW_SECONDS="$3"
STRIDE_SECONDS="$4"
LAYOUT="$5"
MAX_WINDOWS="${6:-}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
cd "${REPO_ROOT}"

PGURL="${PGURL:-postgresql://epilepsiae:epilepsiae@localhost:5432/epilepsiae}"

if command -v python3 >/dev/null 2>&1; then
  PYTHON_BIN="python3"
elif command -v python >/dev/null 2>&1; then
  PYTHON_BIN="python"
else
  echo "[ERROR] Missing dependency: python3 (or python)." >&2
  exit 1
fi

mkdir -p "$(dirname "${OUT}")"

cmd=(
  "${PYTHON_BIN}" -m epilepsiae_sql_dataloader.DataDinghy.ExportChunksToParquet
  --pgurl "${PGURL}"
  --out "${OUT}"
  --patients "${PAT_ID}"
  --states 2
  --data-types 0
  --near-seizure preictal
  --window-seconds "${WINDOW_SECONDS}"
  --stride-seconds "${STRIDE_SECONDS}"
  --layout "${LAYOUT}"
  --verbose
)

if [[ -n "${MAX_WINDOWS}" && "${MAX_WINDOWS}" != "0" ]]; then
  cmd+=(--max-windows-per-patient "${MAX_WINDOWS}")
fi

echo "[INFO] Exporting preictal windows only for patient ${PAT_ID}"
"${cmd[@]}"

rows="$(
"${PYTHON_BIN}" - "${OUT}" <<'PY'
import sys
import pandas as pd

path = sys.argv[1]
df = pd.read_parquet(path)
print(len(df))
PY
)"

if [[ "${rows}" -le 0 ]]; then
  echo "[ERROR] Export produced 0 preictal windows: ${OUT}" >&2
  echo "[ERROR] Try a larger ingestion span or verify patient/state coverage in data_chunks." >&2
  exit 1
fi

echo "[OK] Preictal export complete: rows=${rows}, out=${OUT}"
