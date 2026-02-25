# RUNBOOK — epilepsiae_sql_dataloader

Objetivo: ingestar el dataset Epilepsiae (carpeta `inv/`) a PostgreSQL:
1) Metadata → tablas: `datasets`, `patients`, `samples`, `seizures`
2) Binarios → tabla: `data_chunks` (BYTEA con float32[256] = 1 segundo @ 256 Hz por canal)

> Importante: hoy el loader **no es idempotente** (re-correr puede duplicar o fallar por PK).
> Para re-ejecutar “desde cero”, usar el *Reset dev* (TRUNCATE … RESTART IDENTITY).

---

## 0) Entorno (canónico)

- Conda env: `ieeg-epilepsiae`
- Python: 3.10.x
- DB: PostgreSQL 16 en Docker (container `epilepsiae-db`)
- Variables:
  - `PGURL` : conexión para `psql` y SQLAlchemy (ej: `postgresql://user:pass@host:5432/db`)
  - `DATA_ROOT` : raíz del dataset (contiene `inv/`)
  - `PYTHONNOUSERSITE=1` : evita importar paquetes desde `~/.local`

### 0.1 Activación (siempre en una terminal nueva)

```bash
conda activate ieeg-epilepsiae
export PYTHONNOUSERSITE=1

export PGURL="postgresql://epilepsiae:epilepsiae@localhost:5432/epilepsiae"
export DATA_ROOT="/media/diego/My_Book_Diego/EU_epilepsy_database"