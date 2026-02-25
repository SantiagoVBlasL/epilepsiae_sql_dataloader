# Epilepsiae SQL Loader — Cómo leer y trabajar los datos

Este repo carga el dataset de Epilepsiae (carpetas `pat_*`) a PostgreSQL en dos etapas:

1) **MetaDataBuilder**: crea / puebla metadatos relacionales (datasets, patients, samples, seizures).
2) **PushBinaryToSql (BinaryToSql)**: lee archivos `*.data`, preprocesa (downsample + normalización), parte en chunks de 1 segundo y escribe **DataChunk** (1 fila por canal por segundo).

> Nota: en modo DEBUG (`--max-seconds 10 --max-samples 1`) es normal que **seizure_state salga todo 0**, porque estás leyendo solo los primeros 10 s del primer sample de cada paciente y es muy improbable caer dentro de una crisis.

---

## 0) Setup rápido (env + variables)

En tu shell (ejemplo):

```bash
conda activate ieeg-epilepsiae
export PYTHONNOUSERSITE=1
export PGURL="postgresql://epilepsiae:epilepsiae@localhost:5432/epilepsiae"
export DATA_ROOT="/media/diego/My_Book_Diego/EU_epilepsy_database"
```

---

## 1) Crear / migrar esquema (Alembic)

```bash
alembic upgrade head
alembic current
```

Si `alembic current` muestra `(head)`, estás OK.

---

## 2) Cargar metadatos (datasets/patients/samples/seizures)

```bash
PYTHONPATH=. python epilepsiae_sql_dataloader/RelationalRigging/MetaDataBuilder.py --directory "$DATA_ROOT/inv"
```

Sanity checks:

```bash
psql "$PGURL" -c "select count(*) from datasets;"
psql "$PGURL" -c "select count(*) from patients;"
psql "$PGURL" -c "select count(*) from samples;"
psql "$PGURL" -c "select count(*) from seizures;"
```

---

## 3) Cargar binarios a `data_chunks`

Debug (rápido):

```bash
PYTHONPATH=. python epilepsiae_sql_dataloader/RelationalRigging/PushBinaryToSql.py   --dir "$DATA_ROOT/inv"   --max-samples 1   --max-seconds 10
```

Verificación:

```bash
psql "$PGURL" -c "select count(*) as data_chunks from data_chunks;"
psql "$PGURL" -c "select patient_id, count(*) from data_chunks group by patient_id order by count(*) desc limit 10;"
psql "$PGURL" -c "select seizure_state, count(*) from data_chunks group by seizure_state order by seizure_state;"
psql "$PGURL" -c "select data_type, count(*) from data_chunks group by data_type order by data_type;"
psql "$PGURL" -c "select patient_id, min(octet_length(data)), max(octet_length(data)) from data_chunks group by patient_id order by patient_id limit 5;"
```

- `octet_length(data)=1024` indica `256 muestras * 4 bytes (float32)` por fila (1 segundo a 256 Hz para **un canal**).

---

## 4) Cómo interpretar `data_chunks`

### 4.1 Granularidad de una fila
Cada fila de `data_chunks` representa:

- 1 paciente (`patient_id`)
- 1 canal/electrodo *tipificado* (`data_type`)
- 1 ventana de **1 segundo** a **256 Hz** (256 valores float32)
- estado de crisis (`seizure_state`: 0 normal, 1 ictal, 2 preictal)

El campo `data` se guarda como `BYTEA` (bytes) que se decodifican como `np.float32`.

Ejemplo de decodificación (Python):

```python
import numpy as np
arr = np.frombuffer(row["data"], dtype=np.float32)   # shape (256,)
```

### 4.2 Sobre `data_type`
`data_type` se asigna según dataset y nombre de electrodo. En tu corrida actual, la mayoría queda en `0` (desconocido/otros).
Si querés que la tipificación sea más informativa, normalmente hay que **normalizar/parsear** el nombre del electrodo (p.ej. extraer prefijos tipo `FP`, `F`, `C`, etc. y quitar dígitos/sufijos).

---

## 5) Notebook de QC / exploración
En este repo conviene tener un notebook que:
- Inspeccione el esquema (columnas reales de las tablas).
- Lea algunas filas de `data_chunks`, decodifique el blob y grafique.
- Calcule métricas simples (min/max/mean/std, histograma) para detectar saturación, offset, normalización rara, etc.
- Valide distribución de `seizure_state` y `data_type`.

➡️ Se incluye un notebook base: **01_epilepsiae_sql_qc_preview.ipynb** (ver archivo adjunto / descargable desde este chat).

---

## 6) “Checklist” antes de modo producción
- Confirmar que existe un identificador temporal por chunk (p.ej. `chunk_start_ts`, `sample_id`, `chunk_idx`).
  - Si **no existe**, no vas a poder reconstruir “canales juntos” para un segundo específico sin asumir orden.
- Asegurar batch inserts (no acumular miles/millones de mappings en memoria).
- Revisar índices según queries objetivo (por ejemplo: `(patient_id, seizure_state)`, `(patient_id, data_type)`, y cualquier columna temporal).

