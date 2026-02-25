# Epilepsiae SQL Dataloader (local Postgres)

Pipeline para cargar el dataset Epilepsiae (filesystem + binarios) en PostgreSQL:
1) `MetaDataBuilder.py`: carga metadata (datasets/patients/seizures/samples)
2) `PushBinaryToSql.py`: convierte `.data` a chunks (1s × canal) en `data_chunks`

## Stack
- PostgreSQL 16 (Docker)
- Python 3.8
- SQLAlchemy + psycopg2
- NumPy/SciPy/sklearn

## Entorno
- Postgres corre en Docker (`epilepsiae-db`) expuesto en `localhost:5432`
- Scripts Python corren en el host
- Dataset montado en: `/media/diego/My_Book_Diego/EU_epilepsy_database`

## Quickstart

### 1) Levantar Postgres
(ideal: docker-compose; si no, docker run)
```bash
docker run --name epilepsiae-db \
  -e POSTGRES_USER=epilepsiae \
  -e POSTGRES_PASSWORD=epilepsiae \
  -e POSTGRES_DB=epilepsiae \
  -p 5432:5432 -d postgres:16