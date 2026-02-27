# Architecture

## Overview

`epilepsiae_sql_dataloader` ingests Epilepsiae EEG binaries into PostgreSQL in two stages:

1. Metadata stage
- Reads dataset structure and headers.
- Loads relational metadata into:
`datasets`, `patients`, `samples`, `seizures`.

2. Binary stage
- Reads `.data` binaries from each sample file.
- Downsamples each channel to 256 Hz.
- Normalizes per timepoint across channels.
- Splits into 1-second chunks.
- Writes one row per `(chunk, channel)` into `data_chunks`.

The binary stage is executed by:
`epilepsiae_sql_dataloader.RelationalRigging.PushBinaryToSql`.

## End-to-end ML Pipeline Map

```
pick targets (SQL on samples+seizures)
  -> targeted ingestion (PushBinaryToSql --sample-ids --start-seconds)
  -> chunk table (data_chunks, partitioned, seizure_state labels)
  -> export preictal only   (state=2, near-seizure=preictal)
  -> export interictal only (state=0, near-seizure=none, exclude-near-seizure-seconds=K)
  -> merge + deterministic class balance
  -> final parquet dataset (TCH or CTH_flat)
  -> validation (class counts + row/sample checks)
```

## Pipeline Stages (binary ingestion)

For each sample:

1. Load binary
- Input shape after reshape: `(n_samples, num_channels)`.
- Source dtype: `uint16`.

2. Preprocess
- Cast to `float32`.
- `decimate` from `sample_freq` to `fs_hz` (default 256).
- Normalize rows (`L2`, axis=1).

3. Chunking
- 1-second windows (`n_per_chunk = fs_hz`).
- `num_chunks = num_samples // fs_hz`.
- One row per channel in each chunk.

4. Labeling
- `seizure_state` from seizure windows:
`0=inter-ictal`, `1=ictal`, `2=pre-ictal` (default 3600s before onset).
- `data_type` from electrode naming and dataset family.

5. Insert
- `bulk_insert_mappings` into `data_chunks` in batches.
- Explicit transaction commit per sample ingestion.
- Runtime guardrails raise if 0 rows are generated/inserted.

## Core Tables

Metadata tables:
- `datasets`: dataset catalog (`inv`, `surf`, ...).
- `patients`: patient identity + dataset linkage.
- `samples`: recording-level metadata and path to `.data` binary.
- `seizures`: seizure onset/offset windows.

Signal table:
- `data_chunks`: chunked per-channel signal payload and labels.

## Partitioning Strategy (`data_chunks`)

`data_chunks` is partitioned in PostgreSQL as:

1. `LIST (patient_id)` on root table.
2. Sub-partition `LIST (seizure_state)` under each patient partition.
3. Sub-partition `LIST (data_type)` under each seizure-state partition.

This layout provides:
- Fast patient-scoped filtering.
- Efficient pre-ictal/inter-ictal/ictal slicing.
- Natural pruning for modality filters (`iEEG`, `ECG`, `EKG`, `EEG`).

## ML-ready Key Columns (Alembic rev `2f9eb9c364f5`)

Added columns on `data_chunks`:
- `sample_id`: source sample FK.
- `chunk_idx`: chunk index within sample (seconds from sample start when `sample_length_s=1`).
- `channel_idx`: channel index within sample.
- `electrode_name`: electrode/channel name.
- `chunk_start_ts`: absolute timestamp for chunk start.
- `fs_hz`: sampling rate for stored payload (default 256).

Existing payload/labels kept:
- `patient_id`
- `seizure_state`
- `data_type`
- `data` (`BYTEA`, float32 waveform bytes per chunk/channel)

ML uniqueness constraint:
- `(patient_id, seizure_state, data_type, sample_id, chunk_idx, channel_idx)`

This key prevents duplicate chunk-channel rows while preserving deterministic joins from model outputs back to source recordings.

## Export Pipeline (`ExportChunksToParquet`)

Module:
`epilepsiae_sql_dataloader.DataDinghy.ExportChunksToParquet`

Purpose:
- Read chunk rows from PostgreSQL.
- Decode `BYTEA` payloads back to `float32`.
- Assemble fixed-size sliding windows per `(patient_id, sample_id)`.
- Export window-level examples to Parquet.

Filtering knobs:
- `patients`: selected IDs or `all`.
- `states`: seizure states to include (`0`, `1`, `2`).
- `data_types`: modality filter (`0=iEEG`, `1=ECG`, `2=EKG`, `3=EEG`).
- `max_rows`: debug cap on chunk rows fetched from DB.
- `near_seizure`: optional timestamp filter against `seizures`:
  - `none`: no extra filter.
  - `preictal`: include chunks where `chunk_start_ts in [onset-3600s, onset)`.
  - `ictal`: include chunks that overlap `[onset, offset]`.
- `exclude_near_seizure_seconds` (K): exclusion gate using anti-join (`NOT EXISTS`):
  - when `K=0`, disabled (backward compatible).
  - when `K>0`, drop chunks where `chunk_start_ts in [onset-K, COALESCE(offset,onset)+K]`.

Window construction:
- Input order is deterministic:
`patient_id, sample_id, chunk_idx, channel_idx`.
- For each sample, rows are grouped into chunks and channels.
- Chunk waveforms are decoded as `float32[fs_hz]` (usually `fs_hz=256`).
- Sliding windows are generated with:
`window_seconds` length and `stride_seconds` step.
- Windows crossing chunk-index gaps are dropped.
- Windows with mixed seizure states are dropped.
This keeps a single target label per window.
- Optional deterministic per-patient balancing/size controls:
  - `min_windows_per_patient`
  - `max_windows_per_patient`
  When multiple states are requested, selection is balanced per patient by class.

Output schema (Parquet):
- `patient_id` (`int`)
- `sample_id` (`int`)
- `window_start_chunk_idx` (`int`)
- `window_start_ts` (`timestamp`)
- `seizure_state` (`int`, target)
- `X` (`list[list[list[float]]]`), layout:
  - `TCH`: `[window_seconds][n_channels][fs_hz]`
  - `CTH_flat`: `[n_channels][window_seconds * fs_hz]`
- `electrode_names` (`list[str|None]`) ordered to match channel axis in `X`

Pre-ictal vs inter-ictal semantics:
- `seizure_state` is the class label:
  - `0` = inter-ictal
  - `1` = ictal
  - `2` = pre-ictal
- `near_seizure` is an inclusion gate on top of label filtering:
  - `none`: no extra gate.
  - `preictal`: keep windows/chunks where `chunk_start_ts in [onset-3600s, onset)`.
  - `ictal`: keep windows/chunks that overlap `[onset, offset]`.
- `exclude_near_seizure_seconds` is a separate exclusion gate (anti-join / `NOT EXISTS`):
  - if `K>0`, any chunk too close to any seizure is excluded before window construction.
  - recommended for interictal export: `states=0`, `near_seizure=none`, `exclude_near_seizure_seconds=3600`.

Pipeline B implication:
- A preictal-gated export (`near-seizure=preictal`) should not be expected to contain inter-ictal class `0`.
- For ML-correct binary datasets (`0` vs `2`), export classes separately and balance after export:
  1. preictal-only export (`states=2`, `near-seizure=preictal`)
  2. interictal-only export (`states=0`, `near-seizure=none`)
  3. deterministic merge/balance
- Optional helper `split_by_patient_id(...)` enforces patient-level splits to avoid leakage between train/validation.
