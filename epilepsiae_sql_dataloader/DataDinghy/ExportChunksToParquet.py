"""
Export chunked EEG rows from PostgreSQL into windowed Parquet datasets for ML.

Output schema:
- patient_id
- sample_id
- window_start_chunk_idx
- window_start_ts
- seizure_state
- X (nested list with layout: [window_seconds][n_channels][fs_hz])
- electrode_names (ordered channel names)
"""

from __future__ import annotations

import os
import random
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, Iterator, List, Optional, Sequence, Tuple

import click
import numpy as np
import pandas as pd
from sqlalchemy import and_, create_engine, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.sql.elements import TextClause

from epilepsiae_sql_dataloader.models.LoaderTables import DataChunk
from epilepsiae_sql_dataloader.models.Sample import Sample
from epilepsiae_sql_dataloader.models.Seizures import Seizure
from epilepsiae_sql_dataloader.utils import PGURL, _normalize_pgurl

DEFAULT_STATES = "0,2"
DEFAULT_DATA_TYPES = "0"
DEFAULT_WINDOW_SECONDS = 60
DEFAULT_STRIDE_SECONDS = 10
DEFAULT_NEAR_SEIZURE = "none"
DEFAULT_EXCLUDE_NEAR_SEIZURE_SECONDS = 0
DEFAULT_LAYOUT = "TCH"

PARQUET_COLUMNS = [
    "patient_id",
    "sample_id",
    "window_start_chunk_idx",
    "window_start_ts",
    "seizure_state",
    "X",
    "electrode_names",
]

EXCLUDE_NEAR_SEIZURE_NOT_EXISTS_SQL = """
NOT EXISTS (
  SELECT 1
  FROM seizures z
  WHERE z.pat_id = data_chunks.patient_id
    AND data_chunks.chunk_start_ts >= (z.onset - (:exclude_near_seizure_seconds || ' seconds')::interval)
    AND data_chunks.chunk_start_ts <= (COALESCE(z."offset", z.onset) + (:exclude_near_seizure_seconds || ' seconds')::interval)
)
"""


@dataclass(frozen=True)
class ChunkRow:
    patient_id: int
    sample_id: int
    chunk_idx: int
    channel_idx: int
    electrode_name: Optional[str]
    chunk_start_ts: datetime
    seizure_state: int
    fs_hz: int
    data: bytes
    sample_start_ts: Optional[datetime]
    sample_num_channels: Optional[int]
    sample_elec_names: Optional[str]


@dataclass
class ChunkTensor:
    chunk_idx: int
    chunk_start_ts: datetime
    seizure_state: int
    values: np.ndarray


def parse_int_selector(value: str, *, allow_all: bool, label: str) -> Optional[List[int]]:
    raw = (value or "").strip()
    if not raw:
        raise ValueError(f"{label} cannot be empty")
    if allow_all and raw.lower() == "all":
        return None
    items = [item.strip() for item in raw.split(",") if item.strip()]
    if not items:
        raise ValueError(f"{label} must contain at least one integer")
    return [int(item) for item in items]


def build_exclude_near_seizure_filter(
    exclude_near_seizure_seconds: int,
) -> Tuple[Optional[TextClause], Dict[str, int]]:
    if exclude_near_seizure_seconds < 0:
        raise ValueError(
            f"exclude_near_seizure_seconds must be >= 0, got {exclude_near_seizure_seconds}"
        )
    if exclude_near_seizure_seconds == 0:
        return None, {}

    return text(EXCLUDE_NEAR_SEIZURE_NOT_EXISTS_SQL), {
        "exclude_near_seizure_seconds": int(exclude_near_seizure_seconds)
    }


def decode_bytea_waveform(data: bytes, fs_hz: int) -> np.ndarray:
    if fs_hz <= 0:
        raise ValueError(f"fs_hz must be > 0, got {fs_hz}")
    decoded = np.frombuffer(data, dtype=np.float32)
    if decoded.size != fs_hz:
        raise ValueError(
            f"Invalid chunk waveform length: expected {fs_hz} float32 values, got {decoded.size}"
        )
    return decoded.copy()


def apply_layout(x_tch: np.ndarray, layout: str) -> np.ndarray:
    if layout == "TCH":
        return x_tch
    if layout == "CTH_flat":
        cth = np.transpose(x_tch, (1, 0, 2))  # [C, T, H]
        channels, timesteps, hz = cth.shape
        return cth.reshape(channels, timesteps * hz)
    raise ValueError(f"Unsupported layout: {layout}")


def _finalize_chunk(
    *,
    channel_order: Optional[List[int]],
    channel_values: Dict[int, np.ndarray],
    chunk_idx: Optional[int],
    chunk_start_ts: Optional[datetime],
    seizure_state: Optional[int],
    skipped_incomplete_chunks: int,
) -> Tuple[Optional[ChunkTensor], Optional[List[int]], int]:
    if chunk_idx is None or chunk_start_ts is None or seizure_state is None:
        return None, channel_order, skipped_incomplete_chunks

    if channel_order is None:
        channel_order = sorted(channel_values.keys())
    if set(channel_values.keys()) != set(channel_order):
        return None, channel_order, skipped_incomplete_chunks + 1

    stacked = np.stack([channel_values[idx] for idx in channel_order], axis=0)
    chunk = ChunkTensor(
        chunk_idx=chunk_idx,
        chunk_start_ts=chunk_start_ts,
        seizure_state=seizure_state,
        values=stacked,
    )
    return chunk, channel_order, skipped_incomplete_chunks


def build_windows_for_sample_rows(
    rows: Sequence[ChunkRow],
    *,
    window_seconds: int,
    stride_seconds: int,
    layout: str = DEFAULT_LAYOUT,
) -> Tuple[List[dict], Counter]:
    if window_seconds <= 0:
        raise ValueError(f"window_seconds must be > 0, got {window_seconds}")
    if stride_seconds <= 0:
        raise ValueError(f"stride_seconds must be > 0, got {stride_seconds}")
    if not rows:
        return [], Counter()

    rows_sorted = sorted(rows, key=lambda row: (row.chunk_idx, row.channel_idx))
    first_row = rows_sorted[0]
    fallback_names = []
    if first_row.sample_elec_names:
        fallback_names = Sample.elect_names_to_list(elect_names=first_row.sample_elec_names)

    channel_order: Optional[List[int]] = None
    electrode_by_idx: Dict[int, Optional[str]] = {}
    chunk_tensors: List[ChunkTensor] = []
    stats = Counter()

    current_chunk_idx: Optional[int] = None
    current_chunk_start_ts: Optional[datetime] = None
    current_state: Optional[int] = None
    channel_values: Dict[int, np.ndarray] = {}

    for row in rows_sorted:
        if current_chunk_idx is None:
            current_chunk_idx = row.chunk_idx
            current_chunk_start_ts = row.chunk_start_ts
            current_state = row.seizure_state

        if row.chunk_idx != current_chunk_idx:
            finalized, channel_order, skipped = _finalize_chunk(
                channel_order=channel_order,
                channel_values=channel_values,
                chunk_idx=current_chunk_idx,
                chunk_start_ts=current_chunk_start_ts,
                seizure_state=current_state,
                skipped_incomplete_chunks=stats["skipped_incomplete_chunks"],
            )
            stats["skipped_incomplete_chunks"] = skipped
            if finalized is not None:
                chunk_tensors.append(finalized)

            current_chunk_idx = row.chunk_idx
            current_chunk_start_ts = row.chunk_start_ts
            current_state = row.seizure_state
            channel_values = {}

        if current_state != row.seizure_state:
            raise ValueError(
                f"Inconsistent seizure_state inside chunk: "
                f"patient_id={row.patient_id}, sample_id={row.sample_id}, chunk_idx={row.chunk_idx}"
            )
        if current_chunk_start_ts != row.chunk_start_ts:
            raise ValueError(
                f"Inconsistent chunk_start_ts inside chunk: "
                f"patient_id={row.patient_id}, sample_id={row.sample_id}, chunk_idx={row.chunk_idx}"
            )

        row_fs = int(row.fs_hz) if row.fs_hz is not None else 256
        waveform = decode_bytea_waveform(row.data, fs_hz=row_fs)
        if row.channel_idx in channel_values:
            raise ValueError(
                f"Duplicate channel_idx {row.channel_idx} for "
                f"patient_id={row.patient_id}, sample_id={row.sample_id}, chunk_idx={row.chunk_idx}"
            )

        channel_values[row.channel_idx] = waveform
        electrode_by_idx[row.channel_idx] = row.electrode_name

    finalized, channel_order, skipped = _finalize_chunk(
        channel_order=channel_order,
        channel_values=channel_values,
        chunk_idx=current_chunk_idx,
        chunk_start_ts=current_chunk_start_ts,
        seizure_state=current_state,
        skipped_incomplete_chunks=stats["skipped_incomplete_chunks"],
    )
    stats["skipped_incomplete_chunks"] = skipped
    if finalized is not None:
        chunk_tensors.append(finalized)

    if channel_order is None:
        return [], stats

    electrode_names: List[Optional[str]] = []
    for idx in channel_order:
        fallback_name = fallback_names[idx] if idx < len(fallback_names) else None
        electrode_names.append(electrode_by_idx.get(idx) or fallback_name)

    windows: List[dict] = []
    max_start = len(chunk_tensors) - window_seconds + 1
    if max_start <= 0:
        return windows, stats

    for start in range(0, max_start, stride_seconds):
        segment = chunk_tensors[start : start + window_seconds]
        chunk_indices = [chunk.chunk_idx for chunk in segment]
        expected = list(range(chunk_indices[0], chunk_indices[0] + window_seconds))
        if chunk_indices != expected:
            stats["skipped_non_contiguous_windows"] += 1
            continue

        states = {chunk.seizure_state for chunk in segment}
        if len(states) != 1:
            stats["skipped_mixed_state_windows"] += 1
            continue

        x = np.stack([chunk.values for chunk in segment], axis=0)
        x = apply_layout(x, layout=layout)
        windows.append(
            {
                "patient_id": first_row.patient_id,
                "sample_id": first_row.sample_id,
                "window_start_chunk_idx": chunk_indices[0],
                "window_start_ts": segment[0].chunk_start_ts,
                "seizure_state": segment[0].seizure_state,
                "X": x.tolist(),
                "electrode_names": electrode_names,
            }
        )

    return windows, stats


def split_by_patient_id(
    records: Sequence[dict], *, train_fraction: float = 0.8, seed: int = 42
) -> Tuple[List[dict], List[dict]]:
    if not 0.0 < train_fraction < 1.0:
        raise ValueError(f"train_fraction must be in (0, 1), got {train_fraction}")

    patient_ids = sorted({int(record["patient_id"]) for record in records})
    if len(patient_ids) <= 1:
        return list(records), []

    rng = random.Random(seed)
    rng.shuffle(patient_ids)

    split_at = int(len(patient_ids) * train_fraction)
    split_at = min(max(split_at, 1), len(patient_ids) - 1)
    train_patients = set(patient_ids[:split_at])

    train_records = [record for record in records if int(record["patient_id"]) in train_patients]
    val_records = [record for record in records if int(record["patient_id"]) not in train_patients]
    return train_records, val_records


def _window_sort_key(window: dict) -> Tuple[int, int, datetime, int, int]:
    return (
        int(window["patient_id"]),
        int(window["seizure_state"]),
        window["window_start_ts"],
        int(window["sample_id"]),
        int(window["window_start_chunk_idx"]),
    )


def limit_windows_per_patient(
    windows: Sequence[dict],
    *,
    target_states: Sequence[int],
    min_windows_per_patient: Optional[int],
    max_windows_per_patient: Optional[int],
) -> Tuple[List[dict], Counter]:
    stats = Counter()
    if not windows:
        return [], stats

    min_limit = None if not min_windows_per_patient or min_windows_per_patient <= 0 else int(min_windows_per_patient)
    max_limit = None if not max_windows_per_patient or max_windows_per_patient <= 0 else int(max_windows_per_patient)
    if min_limit is None and max_limit is None:
        return sorted(list(windows), key=_window_sort_key), stats

    required_states = sorted(set(int(state) for state in target_states))
    if not required_states:
        required_states = sorted({int(window["seizure_state"]) for window in windows})

    sorted_windows = sorted(windows, key=_window_sort_key)
    by_patient: Dict[int, List[dict]] = {}
    for window in sorted_windows:
        by_patient.setdefault(int(window["patient_id"]), []).append(window)

    selected: List[dict] = []

    for patient_id in sorted(by_patient):
        patient_windows = by_patient[patient_id]
        by_state: Dict[int, List[dict]] = {state: [] for state in required_states}
        for window in patient_windows:
            state = int(window["seizure_state"])
            if state in by_state:
                by_state[state].append(window)

        if len(required_states) > 1:
            if any(len(by_state[state]) == 0 for state in required_states):
                stats["dropped_patients_missing_state"] += 1
                continue

            per_state_cap = min(len(by_state[state]) for state in required_states)
            if max_limit is not None:
                per_state_cap = min(per_state_cap, max_limit)
            if min_limit is not None and per_state_cap < min_limit:
                stats["dropped_patients_min_windows"] += 1
                continue

            for state in required_states:
                selected.extend(by_state[state][:per_state_cap])
            continue

        primary_state = required_states[0]
        bucket = by_state.get(primary_state, [])
        if min_limit is not None and len(bucket) < min_limit:
            stats["dropped_patients_min_windows"] += 1
            continue

        keep = len(bucket)
        if max_limit is not None:
            keep = min(keep, max_limit)
        selected.extend(bucket[:keep])

    return selected, stats


def _iter_chunk_rows(
    session: Session,
    *,
    patient_ids: Optional[Sequence[int]],
    states: Sequence[int],
    data_types: Sequence[int],
    max_rows: Optional[int],
    near_seizure: str,
    exclude_near_seizure_seconds: int,
) -> Iterator[ChunkRow]:
    query = (
        session.query(
            DataChunk.patient_id.label("patient_id"),
            DataChunk.sample_id.label("sample_id"),
            DataChunk.chunk_idx.label("chunk_idx"),
            DataChunk.channel_idx.label("channel_idx"),
            DataChunk.electrode_name.label("electrode_name"),
            DataChunk.chunk_start_ts.label("chunk_start_ts"),
            DataChunk.seizure_state.label("seizure_state"),
            DataChunk.fs_hz.label("fs_hz"),
            DataChunk.data.label("data"),
            Sample.start_ts.label("sample_start_ts"),
            Sample.num_channels.label("sample_num_channels"),
            Sample.elec_names.label("sample_elec_names"),
        )
        .join(Sample, Sample.id == DataChunk.sample_id)
        .filter(
            DataChunk.sample_id.isnot(None),
            DataChunk.chunk_idx.isnot(None),
            DataChunk.channel_idx.isnot(None),
            DataChunk.chunk_start_ts.isnot(None),
            DataChunk.seizure_state.in_(states),
            DataChunk.data_type.in_(data_types),
        )
        .order_by(
            DataChunk.patient_id.asc(),
            DataChunk.sample_id.asc(),
            DataChunk.chunk_idx.asc(),
            DataChunk.channel_idx.asc(),
        )
    )

    if patient_ids is not None:
        query = query.filter(DataChunk.patient_id.in_(patient_ids))

    if near_seizure == "preictal":
        query = query.join(
            Seizure,
            and_(
                Seizure.pat_id == DataChunk.patient_id,
                DataChunk.chunk_start_ts >= (Seizure.onset - text("INTERVAL '3600 seconds'")),
                DataChunk.chunk_start_ts < Seizure.onset,
            ),
        ).distinct()
    elif near_seizure == "ictal":
        query = query.join(
            Seizure,
            and_(
                Seizure.pat_id == DataChunk.patient_id,
                DataChunk.chunk_start_ts < Seizure.offset,
                (DataChunk.chunk_start_ts + text("INTERVAL '1 second'")) > Seizure.onset,
            ),
        ).distinct()

    exclude_clause, exclude_params = build_exclude_near_seizure_filter(
        exclude_near_seizure_seconds
    )
    if exclude_clause is not None:
        query = query.filter(exclude_clause).params(**exclude_params)

    if max_rows is not None and max_rows > 0:
        query = query.limit(max_rows)

    for row in query.yield_per(5000):
        yield ChunkRow(
            patient_id=int(row.patient_id),
            sample_id=int(row.sample_id),
            chunk_idx=int(row.chunk_idx),
            channel_idx=int(row.channel_idx),
            electrode_name=row.electrode_name,
            chunk_start_ts=row.chunk_start_ts,
            seizure_state=int(row.seizure_state),
            fs_hz=int(row.fs_hz) if row.fs_hz is not None else 256,
            data=bytes(row.data),
            sample_start_ts=row.sample_start_ts,
            sample_num_channels=row.sample_num_channels,
            sample_elec_names=row.sample_elec_names,
        )


def _flush_sample_rows(
    sample_rows: List[ChunkRow],
    *,
    window_seconds: int,
    stride_seconds: int,
    layout: str,
    windows: List[dict],
    stats: Counter,
) -> None:
    if not sample_rows:
        return
    sample_windows, sample_stats = build_windows_for_sample_rows(
        sample_rows,
        window_seconds=window_seconds,
        stride_seconds=stride_seconds,
        layout=layout,
    )
    windows.extend(sample_windows)
    stats.update(sample_stats)


def export_chunks_to_parquet(
    *,
    pgurl: str,
    out_path: str,
    patient_ids: Optional[Sequence[int]],
    states: Sequence[int],
    data_types: Sequence[int],
    window_seconds: int,
    stride_seconds: int,
    max_rows: Optional[int],
    near_seizure: str,
    exclude_near_seizure_seconds: int,
    min_windows_per_patient: Optional[int],
    max_windows_per_patient: Optional[int],
    layout: str,
    verbose: bool,
) -> Tuple[pd.DataFrame, Counter]:
    engine_str = _normalize_pgurl(pgurl)
    engine = create_engine(engine_str)
    SessionLocal = sessionmaker(bind=engine)

    windows: List[dict] = []
    stats: Counter = Counter()
    sample_rows: List[ChunkRow] = []
    current_key: Optional[Tuple[int, int]] = None

    with SessionLocal() as session:
        row_iter = _iter_chunk_rows(
            session,
            patient_ids=patient_ids,
            states=states,
            data_types=data_types,
            max_rows=max_rows,
            near_seizure=near_seizure,
            exclude_near_seizure_seconds=exclude_near_seizure_seconds,
        )
        for row in row_iter:
            key = (row.patient_id, row.sample_id)
            if current_key is not None and key != current_key:
                _flush_sample_rows(
                    sample_rows,
                    window_seconds=window_seconds,
                    stride_seconds=stride_seconds,
                    layout=layout,
                    windows=windows,
                    stats=stats,
                )
                sample_rows = []

            sample_rows.append(row)
            current_key = key

        _flush_sample_rows(
            sample_rows,
            window_seconds=window_seconds,
            stride_seconds=stride_seconds,
            layout=layout,
            windows=windows,
            stats=stats,
        )

    windows, limit_stats = limit_windows_per_patient(
        windows,
        target_states=states,
        min_windows_per_patient=min_windows_per_patient,
        max_windows_per_patient=max_windows_per_patient,
    )
    stats.update(limit_stats)

    class_counts: Counter = Counter(int(window["seizure_state"]) for window in windows)

    df = pd.DataFrame(windows, columns=PARQUET_COLUMNS)

    out_dir = os.path.dirname(out_path)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)

    try:
        df.to_parquet(out_path, index=False)
    except ImportError as exc:
        raise RuntimeError(
            "Parquet export requires 'pyarrow' or 'fastparquet'. "
            "Install one of them in your environment."
        ) from exc

    if verbose:
        print(f"[DEBUG] skipped_incomplete_chunks={stats.get('skipped_incomplete_chunks', 0)}")
        print(
            "[DEBUG] skipped_non_contiguous_windows="
            f"{stats.get('skipped_non_contiguous_windows', 0)}"
        )
        print(
            f"[DEBUG] skipped_mixed_state_windows={stats.get('skipped_mixed_state_windows', 0)}"
        )
        print(
            f"[DEBUG] dropped_patients_missing_state={stats.get('dropped_patients_missing_state', 0)}"
        )
        print(
            f"[DEBUG] dropped_patients_min_windows={stats.get('dropped_patients_min_windows', 0)}"
        )

    return df, class_counts


@click.command()
@click.option(
    "--pgurl",
    default=PGURL,
    show_default=True,
    help="PostgreSQL connection URL (defaults to env PGURL/DATABASE_URL).",
)
@click.option("--out", "out_path", required=True, type=click.Path(dir_okay=False))
@click.option(
    "--patients",
    default="all",
    show_default=True,
    help='Comma-separated patient IDs or "all".',
)
@click.option("--states", default=DEFAULT_STATES, show_default=True, help='Comma-separated seizure states (e.g. "0,2").')
@click.option("--data-types", default=DEFAULT_DATA_TYPES, show_default=True, help='Comma-separated data types (default "0").')
@click.option("--window-seconds", default=DEFAULT_WINDOW_SECONDS, type=int, show_default=True)
@click.option("--stride-seconds", default=DEFAULT_STRIDE_SECONDS, type=int, show_default=True)
@click.option("--max-rows", default=0, type=int, show_default=True, help="Debug cap on fetched chunk rows (0 means no cap).")
@click.option(
    "--near-seizure",
    type=click.Choice(["none", "preictal", "ictal"], case_sensitive=False),
    default=DEFAULT_NEAR_SEIZURE,
    show_default=True,
    help="Timestamp-relative seizure filtering mode using seizures table.",
)
@click.option(
    "--exclude-near-seizure-seconds",
    type=int,
    default=DEFAULT_EXCLUDE_NEAR_SEIZURE_SECONDS,
    show_default=True,
    help=(
        "Exclude chunks whose chunk_start_ts is within "
        "[onset-K, COALESCE(offset,onset)+K] for any seizure of the patient."
    ),
)
@click.option(
    "--min-windows-per-patient",
    default=0,
    type=int,
    show_default=True,
    help="Minimum windows per patient (per class when multiple states are requested).",
)
@click.option(
    "--max-windows-per-patient",
    default=0,
    type=int,
    show_default=True,
    help="Maximum windows per patient (per class when multiple states are requested).",
)
@click.option(
    "--layout",
    type=click.Choice(["TCH", "CTH_flat"], case_sensitive=True),
    default=DEFAULT_LAYOUT,
    show_default=True,
    help="Tensor layout for X.",
)
@click.option("--verbose", is_flag=True, default=False, show_default=True)
def main(
    pgurl: str,
    out_path: str,
    patients: str,
    states: str,
    data_types: str,
    window_seconds: int,
    stride_seconds: int,
    max_rows: int,
    near_seizure: str,
    exclude_near_seizure_seconds: int,
    min_windows_per_patient: int,
    max_windows_per_patient: int,
    layout: str,
    verbose: bool,
) -> None:
    patient_ids = parse_int_selector(patients, allow_all=True, label="patients")
    state_ids = parse_int_selector(states, allow_all=False, label="states")
    data_type_ids = parse_int_selector(data_types, allow_all=False, label="data-types")
    row_cap = None if max_rows <= 0 else max_rows

    if verbose:
        click.echo(f"[DEBUG] pgurl={_normalize_pgurl(pgurl)}")
        click.echo(f"[DEBUG] out={out_path}")
        click.echo(f"[DEBUG] patients={patient_ids if patient_ids is not None else 'all'}")
        click.echo(f"[DEBUG] states={state_ids}")
        click.echo(f"[DEBUG] data_types={data_type_ids}")
        click.echo(f"[DEBUG] window_seconds={window_seconds}, stride_seconds={stride_seconds}")
        click.echo(f"[DEBUG] max_rows={row_cap}")
        click.echo(f"[DEBUG] near_seizure={near_seizure}")
        click.echo(
            f"[DEBUG] exclude_near_seizure_seconds={exclude_near_seizure_seconds}"
        )
        click.echo(
            f"[DEBUG] min_windows_per_patient={min_windows_per_patient}, "
            f"max_windows_per_patient={max_windows_per_patient}"
        )
        click.echo(f"[DEBUG] layout={layout}")

    df, class_counts = export_chunks_to_parquet(
        pgurl=pgurl,
        out_path=out_path,
        patient_ids=patient_ids,
        states=state_ids or [],
        data_types=data_type_ids or [],
        window_seconds=window_seconds,
        stride_seconds=stride_seconds,
        max_rows=row_cap,
        near_seizure=near_seizure.lower(),
        exclude_near_seizure_seconds=exclude_near_seizure_seconds,
        min_windows_per_patient=min_windows_per_patient,
        max_windows_per_patient=max_windows_per_patient,
        layout=layout,
        verbose=verbose,
    )

    click.echo(f"Exported {len(df)} windows to {out_path}")
    if class_counts:
        for state in sorted(class_counts):
            click.echo(f"class {state}: {class_counts[state]} windows")
    else:
        for state in sorted(state_ids):
            click.echo(f"class {state}: 0 windows")


if __name__ == "__main__":
    main()
