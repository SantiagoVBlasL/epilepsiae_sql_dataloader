#!/usr/bin/env bash

# Source this file from your shell:
#   source tools/dev/env.sh

export PYTHONNOUSERSITE="${PYTHONNOUSERSITE:-1}"
export PGURL="${PGURL:-postgresql://epilepsiae:epilepsiae@localhost:5432/epilepsiae}"
export DATA_ROOT="${DATA_ROOT:-/media/diego/My_Book_Diego/EU_epilepsy_database}"

export EPILEPSIAE_DB_CONTAINER="${EPILEPSIAE_DB_CONTAINER:-epilepsiae-db}"
export EPILEPSIAE_DB_VOLUME="${EPILEPSIAE_DB_VOLUME:-epilepsiae-db-data}"
