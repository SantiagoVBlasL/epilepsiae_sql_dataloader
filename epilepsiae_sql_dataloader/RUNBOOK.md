# RUNBOOK - epilepsiae_sql_dataloader

Objetivo: ingestar el dataset Epilepsiae (carpeta `inv/`) a PostgreSQL.

1. Metadata -> tablas: `datasets`, `patients`, `samples`, `seizures`
2. Binarios -> tabla: `data_chunks` (BYTEA con float32[256] = 1 segundo @ 256 Hz por canal)

## 0) Entorno canonico

- Conda env: `ieeg-epilepsiae`
- Python: 3.10.x
- DB: PostgreSQL 16
- Variables:
`PGURL` (conexion DB), `DATA_ROOT` (raiz del dataset), `PYTHONNOUSERSITE=1`

```bash
conda activate ieeg-epilepsiae
export PYTHONNOUSERSITE=1
export PGURL="postgresql://epilepsiae:epilepsiae@localhost:5432/epilepsiae"
export DATA_ROOT="/media/diego/My_Book_Diego/EU_epilepsy_database"
```

### 0.1 Reboot-proof bootstrap (Docker + env + sanity)

```bash
bash tools/dev/up.sh
source tools/dev/env.sh
bash tools/dev/status.sh
```

Notebook de chequeo rapido:

```bash
jupyter notebook notebooks/00_environment_sanity.ipynb
```

Para apagar DB local sin borrar datos:

```bash
bash tools/dev/down.sh
```

Para borrar explicitamente contenedor + volumen (destructivo):

```bash
bash tools/dev/down.sh --purge --yes
```

## 1) Debug ingestion

Ejecutar una corrida minima (1 paciente, 1 sample, 10 segundos) con logs verbosos:

```bash
python -m epilepsiae_sql_dataloader.RelationalRigging.PushBinaryToSql \
  --dir "$DATA_ROOT/inv" \
  --max-patients 1 \
  --max-samples 1 \
  --max-seconds 10 \
  --verbose
```

Modo simulacion (build de mappings, sin insertar):

```bash
python -m epilepsiae_sql_dataloader.RelationalRigging.PushBinaryToSql \
  --dir "$DATA_ROOT/inv" \
  --max-patients 1 \
  --max-samples 1 \
  --max-seconds 10 \
  --verbose \
  --dry-run
```

En `--verbose` se imprimen:
- `ENGINE_STR` usado
- `start_seconds` y `seek_bytes`
- `binary_data.shape` y `down_sampled.shape`
- `num_chunks` y `expected_rows`
- `len(data_chunks)` antes de cada `bulk_insert_mappings`
- conteo visible en la misma transaccion:
`SELECT COUNT(*) FROM data_chunks WHERE patient_id = :pid`

Si no se generan filas, el loader falla de forma explicita (no hay "success" silencioso).

### 1.1 Ingesta enfocada en pre-ictal (sin leer toda la grabacion)

Para apuntar al periodo pre-ictal del primer evento en un sample:

`start_seconds = max(0, int((onset - sample.start_ts).total_seconds()) - 3600 - margin)`

Donde `margin` puede ser `120` segundos.

Ejemplo:

```bash
python -m epilepsiae_sql_dataloader.RelationalRigging.PushBinaryToSql \
  --dir "$DATA_ROOT/inv" \
  --max-patients 1 \
  --max-samples 1 \
  --start-seconds 3480 \
  --max-seconds 600 \
  --verbose
```

Interpretacion:
- salta directo cerca de `onset-3600s`
- lee solo `max_seconds` desde ese offset
- permite generar `seizure_state=2` sin cargar horas completas

### 1.2 Targeted preictal ingestion by sample_id

Primero, obtener candidatos (sample que intersecta la ventana pre-ictal):

```bash
psql "$PGURL" -c "
with candidates as (
  select
    s.pat_id,
    sm.id as sample_id,
    sm.start_ts,
    sm.duration_in_sec,
    z.onset,
    z.offset,
    greatest(
      0,
      extract(
        epoch from
        least(sm.start_ts + (sm.duration_in_sec || ' seconds')::interval, z.onset)
        - greatest(sm.start_ts, z.onset - interval '3600 seconds')
      )
    )::int as preictal_overlap_s
  from seizures z
  join samples sm on sm.pat_id = z.pat_id
  join patients s on s.id = sm.pat_id
  where sm.start_ts < z.onset
    and (sm.start_ts + (sm.duration_in_sec || ' seconds')::interval) > (z.onset - interval '3600 seconds')
)
select *
from candidates
where preictal_overlap_s > 0
order by preictal_overlap_s desc
limit 50;"
```

Luego, ingestar solo un sample puntual con `--sample-ids`:

```bash
python -m epilepsiae_sql_dataloader.RelationalRigging.PushBinaryToSql \
  --dir "$DATA_ROOT/inv" \
  --max-patients 1 \
  --sample-ids 646 \
  --start-seconds 0 \
  --max-seconds 1200 \
  --verbose
```

Ejemplo concreto de candidato reportado por la consulta: `pat_id=253`, `sample_id=646`, `start_seconds=0`, `max_seconds=1200`.

Patron recomendado para `start_seconds` por candidato:

`start_seconds = max(0, int((onset - sample.start_ts).total_seconds()) - 3600 - margin)`

Con `margin = 120` como valor inicial.

## 2) Validacion SQL de filas y particiones

Conteo total (tabla padre; en PostgreSQL particionado incluye hijos):

```bash
psql "$PGURL" -c "select count(*) as total_rows from data_chunks;"
```

Conteo por paciente (desde tabla padre):

```bash
psql "$PGURL" -c "
select patient_id, count(*) as rows
from data_chunks
group by patient_id
order by patient_id;"
```

Conteo exacto por particion para un paciente (usa `tableoid`):

```bash
psql "$PGURL" -c "
select tableoid::regclass as partition_name, count(*) as rows
from data_chunks
where patient_id = 108402
group by tableoid
order by rows desc;"
```

Listar arbol de particiones:

```bash
psql "$PGURL" -c "
select relid::regclass as relname, parentrelid::regclass as parent
from pg_partition_tree('data_chunks')
order by 2 nulls first, 1;"
```

Conteo exacto por cada particion (comando para `psql`, usa `\gexec`):

```bash
psql "$PGURL" <<'SQL'
SELECT format(
  'SELECT %L AS partition_name, count(*) AS rows FROM %s;',
  c.relname,
  c.oid::regclass
)
FROM pg_class c
JOIN pg_inherits i ON i.inhrelid = c.oid
WHERE i.inhparent = 'data_chunks'::regclass
ORDER BY c.relname;
\gexec
SQL
```

## 3) Checks de duplicados utiles

Seizures duplicadas por `(pat_id, onset, offset)`:

```bash
psql "$PGURL" -c '
select pat_id, onset, "offset" as offset_ts, count(*) as n
from seizures
group by pat_id, onset, "offset"
having count(*) > 1
order by n desc
limit 20;'
```

Samples duplicadas por `data_file`:

```bash
psql "$PGURL" -c '
select data_file, count(*) as n
from samples
group by data_file
having count(*) > 1
order by n desc
limit 20;'
```

## 4) Export ML windows to Parquet

Export debug subset (pre-ictal vs inter-ictal, iEEG only):

```bash
python -m epilepsiae_sql_dataloader.DataDinghy.ExportChunksToParquet \
  --pgurl "$PGURL" \
  --out ./exports/preictal_vs_interictal_debug.parquet \
  --patients all \
  --states 0,2 \
  --data-types 0 \
  --window-seconds 60 \
  --stride-seconds 10 \
  --max-rows 10000 \
  --verbose
```

Balanced pre-ictal vs inter-ictal export (deterministic per patient):

```bash
python -m epilepsiae_sql_dataloader.DataDinghy.ExportChunksToParquet \
  --pgurl "$PGURL" \
  --out ./exports/preictal_vs_interictal_balanced.parquet \
  --patients all \
  --states 0,2 \
  --data-types 0 \
  --near-seizure preictal \
  --window-seconds 5 \
  --stride-seconds 2 \
  --min-windows-per-patient 1 \
  --max-windows-per-patient 20 \
  --layout CTH_flat \
  --max-rows 10000 \
  --verbose
```

Expected console output:
- total exported window count
- class counts per `seizure_state` (for example: `class 0: ...`, `class 2: ...`)
- when `--near-seizure` is used, rows are time-filtered by `seizures.onset/offset`

Output schema (`Parquet`):
- `patient_id`
- `sample_id`
- `window_start_chunk_idx`
- `window_start_ts`
- `seizure_state` (target)
- `X` layout is configurable:
  - `TCH`: `[window_seconds][n_channels][fs_hz]`
  - `CTH_flat`: `[n_channels][window_seconds*fs_hz]`
- `electrode_names` aligned with channel dimension of `X`

Label meaning for binary pre-ictal vs inter-ictal:
- `0` = inter-ictal
- `2` = pre-ictal

Notes:
- Windows with mixed seizure states are dropped to keep one unambiguous target per window.
- Windows with chunk-index gaps are dropped.
- `--near-seizure preictal`: keeps chunks with `chunk_start_ts in [onset-3600s, onset)`.
- `--near-seizure ictal`: keeps chunks overlapping `[onset, offset]`.
- `--min-windows-per-patient` and `--max-windows-per-patient` apply deterministic per-patient controls; with multiple states they are balanced per class.
- For train/val/test, split by `patient_id` to avoid leakage across recordings from the same patient.

## 5) End-to-end pipeline (targets -> ingest -> export -> validate)

### 5.1 Pick targets (SQL candidate query)

Use this query to list candidate `(pat_id, sample_id)` windows that overlap a seizure pre-ictal range:

```bash
psql "$PGURL" -c "
with candidates as (
  select
    z.pat_id,
    sm.id as sample_id,
    z.onset,
    extract(epoch from (z.onset - sm.start_ts))::int as onset_sec_into_sample,
    greatest(
      0,
      extract(
        epoch from
        least(sm.start_ts + (sm.duration_in_sec || ' seconds')::interval, z.onset)
        - greatest(sm.start_ts, z.onset - interval '3600 seconds')
      )
    )::int as preictal_overlap_s
  from seizures z
  join samples sm on sm.pat_id = z.pat_id
  where sm.start_ts < z.onset
    and (sm.start_ts + (sm.duration_in_sec || ' seconds')::interval) > (z.onset - interval '3600 seconds')
)
select pat_id, sample_id, onset, onset_sec_into_sample, preictal_overlap_s
from candidates
where preictal_overlap_s >= (60 + 120)
order by preictal_overlap_s desc, pat_id, sample_id
limit 100;"
```

Rule of thumb:
- Require `preictal_overlap_s >= window_seconds + margin`.
- For `window_seconds=60` and `margin=120`, use `>= 180`.

### 5.2 Targeted ingestion (sample_id + start_seconds)

Compute:

`start_seconds = max(0, onset_sec_into_sample - 3600 - margin)` with `margin=120`.

Ingest only one chosen sample:

```bash
PAT_ID=253
SAMPLE_ID=646
START_SECONDS=0
LEN_SECONDS=1200

python -m epilepsiae_sql_dataloader.RelationalRigging.PushBinaryToSql \
  --dir "$DATA_ROOT/inv" \
  --max-patients 1 \
  --sample-ids "$SAMPLE_ID" \
  --start-seconds "$START_SECONDS" \
  --max-seconds "$LEN_SECONDS" \
  --verbose
```

Validate class-2 rows exist after ingestion:

```bash
psql "$PGURL" -c "
select patient_id, sample_id, seizure_state, count(*) as rows
from data_chunks
where patient_id = $PAT_ID
  and sample_id = $SAMPLE_ID
group by 1,2,3
order by seizure_state;"
```

### 5.3 Export balanced Parquet (pre-ictal vs inter-ictal)

```bash
PAT_ID=253
OUT=./exports/pat_${PAT_ID}_preictal_vs_interictal_w60_s10.parquet

python -m epilepsiae_sql_dataloader.DataDinghy.ExportChunksToParquet \
  --pgurl "$PGURL" \
  --out "$OUT" \
  --patients "$PAT_ID" \
  --states 0,2 \
  --data-types 0 \
  --near-seizure preictal \
  --window-seconds 60 \
  --stride-seconds 10 \
  --layout CTH_flat \
  --min-windows-per-patient 1 \
  --max-windows-per-patient 50 \
  --verbose
```

### 5.4 Validate export

Exporter already prints class counts per `seizure_state`.

Quick Parquet check:

```bash
python - <<'PY'
import numpy as np
import pandas as pd

out = "./exports/pat_253_preictal_vs_interictal_w60_s10.parquet"
df = pd.read_parquet(out)
print(df["seizure_state"].value_counts().sort_index())
print("rows:", len(df))
print("X[0] shape:", np.asarray(df.iloc[0]["X"]).shape)
PY
```
