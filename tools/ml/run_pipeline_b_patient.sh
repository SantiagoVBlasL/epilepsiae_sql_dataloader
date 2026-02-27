#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage:
  tools/ml/run_pipeline_b_patient.sh PAT_ID [WINDOW_SECONDS] [STRIDE_SECONDS] [LAYOUT] [MAX_PRE] [MAX_INT] [SEED] [EXCLUDE_INT_NEAR_SEIZURE_SECONDS]

Defaults:
  WINDOW_SECONDS=60
  STRIDE_SECONDS=10
  LAYOUT=CTH_flat
  MAX_PRE=200
  MAX_INT=200
  SEED=42
  EXCLUDE_INT_NEAR_SEIZURE_SECONDS=3600
USAGE
}

if [[ $# -lt 1 || $# -gt 8 ]]; then
  usage
  exit 1
fi

PAT_ID="$1"
WINDOW_SECONDS="${2:-60}"
STRIDE_SECONDS="${3:-10}"
LAYOUT="${4:-CTH_flat}"
MAX_PRE="${5:-200}"
MAX_INT="${6:-200}"
SEED="${7:-42}"
EXCLUDE_INT_NEAR_SEIZURE_SECONDS="${8:-3600}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
cd "${REPO_ROOT}"

require_cmd() {
  local cmd="$1"
  if ! command -v "${cmd}" >/dev/null 2>&1; then
    echo "[ERROR] Missing dependency: ${cmd}" >&2
    exit 1
  fi
}

require_cmd bash

if command -v python3 >/dev/null 2>&1; then
  PYTHON_BIN="python3"
elif command -v python >/dev/null 2>&1; then
  PYTHON_BIN="python"
else
  echo "[ERROR] Missing dependency: python3 (or python)." >&2
  exit 1
fi

mkdir -p "${REPO_ROOT}/exports"

PRE_OUT="${REPO_ROOT}/exports/pat_${PAT_ID}_preictal_only_w${WINDOW_SECONDS}_s${STRIDE_SECONDS}_${LAYOUT}.parquet"
INT_OUT="${REPO_ROOT}/exports/pat_${PAT_ID}_interictal_only_w${WINDOW_SECONDS}_s${STRIDE_SECONDS}_${LAYOUT}.parquet"
BAL_OUT="${REPO_ROOT}/exports/pat_${PAT_ID}_balanced_w${WINDOW_SECONDS}_s${STRIDE_SECONDS}_${LAYOUT}.parquet"

echo "[STEP] Exporting PREICTAL windows only (state=2, near-seizure=preictal)"
bash "${SCRIPT_DIR}/export_preictal_only.sh" \
  "${PAT_ID}" \
  "${PRE_OUT}" \
  "${WINDOW_SECONDS}" \
  "${STRIDE_SECONDS}" \
  "${LAYOUT}" \
  "${MAX_PRE}"

echo "[STEP] Exporting INTERICTAL windows only (state=0, near-seizure=none)"
bash "${SCRIPT_DIR}/export_interictal_only.sh" \
  "${PAT_ID}" \
  "${INT_OUT}" \
  "${WINDOW_SECONDS}" \
  "${STRIDE_SECONDS}" \
  "${LAYOUT}" \
  "${MAX_INT}" \
  "${EXCLUDE_INT_NEAR_SEIZURE_SECONDS}"

echo "[STEP] Merging and balancing Parquets"
"${PYTHON_BIN}" "${SCRIPT_DIR}/merge_balance_parquets.py" \
  --preictal "${PRE_OUT}" \
  --interictal "${INT_OUT}" \
  --out "${BAL_OUT}" \
  --seed "${SEED}" \
  --strategy downsample

echo "[STEP] Final validation"
"${PYTHON_BIN}" - "${BAL_OUT}" <<'PY'
import sys
import numpy as np
import pandas as pd

path = sys.argv[1]
df = pd.read_parquet(path)
if df.empty:
    raise RuntimeError(f"Balanced parquet is empty: {path}")

print(df["seizure_state"].value_counts().sort_index())
print(f"rows: {len(df)}")
print(f"X[0] shape: {np.asarray(df.iloc[0]['X']).shape}")
PY

echo
echo "[OK] Pipeline B finished for patient ${PAT_ID}"
echo "[OUT] preictal:   ${PRE_OUT}"
echo "[OUT] interictal: ${INT_OUT}"
echo "[OUT] balanced:   ${BAL_OUT}"
