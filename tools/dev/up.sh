#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./env.sh
source "${SCRIPT_DIR}/env.sh"

require_cmd() {
  local cmd="$1"
  if ! command -v "${cmd}" >/dev/null 2>&1; then
    echo "[ERROR] Missing dependency: ${cmd}" >&2
    echo "[ERROR] Install '${cmd}' and re-run tools/dev/up.sh." >&2
    exit 1
  fi
}

for cmd in docker psql pg_isready; do
  require_cmd "${cmd}"
done

container_exists() {
  docker ps -a --format '{{.Names}}' | grep -Fxq "${EPILEPSIAE_DB_CONTAINER}"
}

ensure_volume() {
  if docker volume inspect "${EPILEPSIAE_DB_VOLUME}" >/dev/null 2>&1; then
    return
  fi
  echo "[INFO] Creating Docker volume: ${EPILEPSIAE_DB_VOLUME}"
  docker volume create "${EPILEPSIAE_DB_VOLUME}" >/dev/null
}

ensure_container_running() {
  if container_exists; then
    local status
    status="$(docker inspect -f '{{.State.Status}}' "${EPILEPSIAE_DB_CONTAINER}")"
    if [[ "${status}" == "running" ]]; then
      echo "[INFO] Container '${EPILEPSIAE_DB_CONTAINER}' is already running."
      return
    fi

    echo "[INFO] Starting existing container '${EPILEPSIAE_DB_CONTAINER}' (status=${status})."
    docker start "${EPILEPSIAE_DB_CONTAINER}" >/dev/null
    return
  fi

  ensure_volume
  echo "[INFO] Creating and starting container '${EPILEPSIAE_DB_CONTAINER}' (postgres:16)."
  docker run -d \
    --name "${EPILEPSIAE_DB_CONTAINER}" \
    -e POSTGRES_USER=epilepsiae \
    -e POSTGRES_PASSWORD=epilepsiae \
    -e POSTGRES_DB=epilepsiae \
    -p 5432:5432 \
    -v "${EPILEPSIAE_DB_VOLUME}:/var/lib/postgresql/data" \
    postgres:16 >/dev/null
}

wait_for_db() {
  echo "[INFO] Waiting for PostgreSQL readiness..."
  local i
  for i in $(seq 1 60); do
    if pg_isready -d "${PGURL}" >/dev/null 2>&1; then
      echo "[OK] PostgreSQL is ready."
      return
    fi
    sleep 1
  done

  echo "[ERROR] PostgreSQL did not become ready after 60 seconds." >&2
  echo "[ERROR] Check container logs: docker logs ${EPILEPSIAE_DB_CONTAINER}" >&2
  exit 1
}

run_check() {
  local label="$1"
  local sql="$2"
  echo "[CHECK] ${label}"
  psql "${PGURL}" -v ON_ERROR_STOP=1 -c "${sql}"
}

ensure_container_running
wait_for_db

echo
echo "[INFO] Export these in your current shell:"
echo "export PYTHONNOUSERSITE=${PYTHONNOUSERSITE}"
echo "export PGURL=${PGURL}"
echo "export DATA_ROOT=${DATA_ROOT}"
echo
echo "[INFO] Or run: source tools/dev/env.sh"
echo

echo "[INFO] Docker status"
docker ps --filter "name=^/${EPILEPSIAE_DB_CONTAINER}$" --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'
echo

run_check "DB clock" "select now();"
run_check "Alembic head" "select max(version_num) as alembic_head from alembic_version;"
run_check "samples count" "select count(*) as samples from samples;"
run_check "data_chunks count" "select count(*) as data_chunks from data_chunks;"
run_check "seizure_state distribution" "select seizure_state, count(*) as rows from data_chunks group by 1 order by 1;"
run_check "patients present in data_chunks" "select count(distinct patient_id) as patients_in_data_chunks from data_chunks;"

echo
echo "[OK] tools/dev/up.sh completed."
