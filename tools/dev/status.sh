#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./env.sh
source "${SCRIPT_DIR}/env.sh"

require_cmd() {
  local cmd="$1"
  if ! command -v "${cmd}" >/dev/null 2>&1; then
    echo "[ERROR] Missing dependency: ${cmd}" >&2
    echo "[ERROR] Install '${cmd}' and re-run tools/dev/status.sh." >&2
    exit 1
  fi
}

for cmd in docker psql pg_isready; do
  require_cmd "${cmd}"
done

run_sql() {
  local label="$1"
  local sql="$2"
  echo "[SQL] ${label}"
  psql "${PGURL}" -v ON_ERROR_STOP=1 -c "${sql}"
}

echo "[INFO] Environment"
echo "PGURL=${PGURL}"
echo "DATA_ROOT=${DATA_ROOT}"
echo "PYTHONNOUSERSITE=${PYTHONNOUSERSITE}"
echo

echo "[INFO] Docker container status"
if docker ps -a --format '{{.Names}}' | grep -Fxq "${EPILEPSIAE_DB_CONTAINER}"; then
  docker ps -a --filter "name=^/${EPILEPSIAE_DB_CONTAINER}$" --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'
else
  echo "[WARN] Container '${EPILEPSIAE_DB_CONTAINER}' does not exist."
fi
echo

echo "[INFO] pg_isready"
if pg_isready -d "${PGURL}" >/dev/null 2>&1; then
  echo "[OK] Database is ready."
else
  echo "[ERROR] Database is not ready at ${PGURL}" >&2
  echo "[ERROR] Try: tools/dev/up.sh" >&2
  exit 1
fi
echo

run_sql "Alembic head" "select max(version_num) as alembic_head from alembic_version;"
run_sql "Core counts" "select (select count(*) from samples) as samples, (select count(*) from seizures) as seizures, (select count(*) from data_chunks) as data_chunks;"
run_sql "seizure_state distribution" "select seizure_state, count(*) as rows from data_chunks group by 1 order by 1;"
run_sql "Top patients by rows" "select patient_id, count(*) as rows from data_chunks group by 1 order by rows desc limit 20;"

echo
echo "[OK] tools/dev/status.sh completed."
