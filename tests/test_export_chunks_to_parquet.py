import os
from datetime import datetime, timedelta

import numpy as np
import pytest

from epilepsiae_sql_dataloader.DataDinghy.ExportChunksToParquet import (
    ChunkRow,
    EXCLUDE_NEAR_SEIZURE_NOT_EXISTS_SQL,
    build_exclude_near_seizure_filter,
    build_windows_for_sample_rows,
    decode_bytea_waveform,
    export_chunks_to_parquet,
    limit_windows_per_patient,
    split_by_patient_id,
)
from epilepsiae_sql_dataloader.utils import PGURL


def test_decode_bytea_waveform_to_float32_vector():
    expected = np.arange(256, dtype=np.float32)
    decoded = decode_bytea_waveform(expected.tobytes(), fs_hz=256)

    assert decoded.shape == (256,)
    assert decoded.dtype == np.float32
    np.testing.assert_array_equal(decoded, expected)


def test_build_windows_for_sample_rows_shape():
    start_ts = datetime(2024, 1, 1, 0, 0, 0)
    rows = []

    for chunk_idx in (0, 1):
        for channel_idx in (0, 1):
            values = np.full((256,), fill_value=chunk_idx * 10 + channel_idx, dtype=np.float32)
            rows.append(
                ChunkRow(
                    patient_id=11,
                    sample_id=22,
                    chunk_idx=chunk_idx,
                    channel_idx=channel_idx,
                    electrode_name=f"E{channel_idx}",
                    chunk_start_ts=start_ts + timedelta(seconds=chunk_idx),
                    seizure_state=2,
                    fs_hz=256,
                    data=values.tobytes(),
                    sample_start_ts=start_ts,
                    sample_num_channels=2,
                    sample_elec_names="[E0,E1]",
                )
            )

    windows, stats = build_windows_for_sample_rows(
        rows,
        window_seconds=2,
        stride_seconds=1,
    )

    assert stats["skipped_mixed_state_windows"] == 0
    assert len(windows) == 1

    x = np.asarray(windows[0]["X"], dtype=np.float32)
    assert x.shape == (2, 2, 256)
    assert windows[0]["seizure_state"] == 2
    assert windows[0]["electrode_names"] == ["E0", "E1"]
    assert x[0, 0, 0] == 0.0
    assert x[1, 1, 0] == 11.0


def test_build_windows_for_sample_rows_cth_flat_layout():
    start_ts = datetime(2024, 1, 1, 0, 0, 0)
    rows = []

    for chunk_idx in (0, 1):
        for channel_idx in (0, 1):
            values = np.full((256,), fill_value=chunk_idx * 10 + channel_idx, dtype=np.float32)
            rows.append(
                ChunkRow(
                    patient_id=11,
                    sample_id=22,
                    chunk_idx=chunk_idx,
                    channel_idx=channel_idx,
                    electrode_name=f"E{channel_idx}",
                    chunk_start_ts=start_ts + timedelta(seconds=chunk_idx),
                    seizure_state=2,
                    fs_hz=256,
                    data=values.tobytes(),
                    sample_start_ts=start_ts,
                    sample_num_channels=2,
                    sample_elec_names="[E0,E1]",
                )
            )

    windows, _ = build_windows_for_sample_rows(
        rows,
        window_seconds=2,
        stride_seconds=1,
        layout="CTH_flat",
    )

    assert len(windows) == 1
    x = np.asarray(windows[0]["X"], dtype=np.float32)
    assert x.shape == (2, 512)
    assert x[0, 0] == 0.0
    assert x[0, 256] == 10.0


def test_limit_windows_per_patient_balances_classes_deterministically():
    windows = []
    for idx in range(5):
        windows.append(
            {
                "patient_id": 1,
                "sample_id": 10,
                "window_start_chunk_idx": idx,
                "window_start_ts": datetime(2024, 1, 1, 0, 0, 0) + timedelta(seconds=idx),
                "seizure_state": 0,
                "X": [],
                "electrode_names": [],
            }
        )
    for idx in range(3):
        windows.append(
            {
                "patient_id": 1,
                "sample_id": 10,
                "window_start_chunk_idx": 100 + idx,
                "window_start_ts": datetime(2024, 1, 1, 0, 10, 0) + timedelta(seconds=idx),
                "seizure_state": 2,
                "X": [],
                "electrode_names": [],
            }
        )

    limited, stats = limit_windows_per_patient(
        windows,
        target_states=[0, 2],
        min_windows_per_patient=2,
        max_windows_per_patient=2,
    )

    assert stats["dropped_patients_missing_state"] == 0
    assert stats["dropped_patients_min_windows"] == 0
    assert len(limited) == 4

    states = [row["seizure_state"] for row in limited]
    assert states.count(0) == 2
    assert states.count(2) == 2
    assert [row["window_start_chunk_idx"] for row in limited] == [0, 1, 100, 101]


def test_split_by_patient_id_has_no_leakage():
    records = [
        {"patient_id": 1, "sample_id": 10},
        {"patient_id": 1, "sample_id": 11},
        {"patient_id": 2, "sample_id": 20},
        {"patient_id": 3, "sample_id": 30},
        {"patient_id": 4, "sample_id": 40},
        {"patient_id": 4, "sample_id": 41},
    ]

    train, val = split_by_patient_id(records, train_fraction=0.5, seed=123)
    train_patients = {record["patient_id"] for record in train}
    val_patients = {record["patient_id"] for record in val}

    assert train_patients
    assert val_patients
    assert train_patients.isdisjoint(val_patients)
    assert len(train) + len(val) == len(records)


def test_build_exclude_near_seizure_filter_disabled_when_zero():
    clause, params = build_exclude_near_seizure_filter(0)
    assert clause is None
    assert params == {}


def test_build_exclude_near_seizure_filter_includes_not_exists_and_offset_param():
    clause, params = build_exclude_near_seizure_filter(3600)
    assert clause is not None
    assert "NOT EXISTS" in clause.text
    assert 'z."offset"' in clause.text
    assert "COALESCE" in clause.text
    assert params == {"exclude_near_seizure_seconds": 3600}
    assert (
        "(:exclude_near_seizure_seconds || ' seconds')::interval"
        in EXCLUDE_NEAR_SEIZURE_NOT_EXISTS_SQL
    )


def test_build_exclude_near_seizure_filter_rejects_negative_values():
    with pytest.raises(ValueError, match="exclude_near_seizure_seconds must be >= 0"):
        build_exclude_near_seizure_filter(-1)


@pytest.mark.skipif(os.getenv("RUN_DB_TESTS") != "1", reason="Set RUN_DB_TESTS=1 to enable DB integration tests")
def test_export_exclusion_gate_reduces_or_keeps_interictal_count():
    if not PGURL:
        pytest.skip("PGURL is not configured")

    df_base, _ = export_chunks_to_parquet(
        pgurl=PGURL,
        out_path="/tmp/export_base_interictal.parquet",
        patient_ids=[548],
        states=[0],
        data_types=[0],
        window_seconds=60,
        stride_seconds=10,
        max_rows=5000,
        near_seizure="none",
        exclude_near_seizure_seconds=0,
        min_windows_per_patient=0,
        max_windows_per_patient=0,
        layout="TCH",
        verbose=False,
    )
    if len(df_base) == 0:
        pytest.skip("No baseline windows found for patient_id=548, state=0 in this DB")

    df_excluded, _ = export_chunks_to_parquet(
        pgurl=PGURL,
        out_path="/tmp/export_excluded_interictal.parquet",
        patient_ids=[548],
        states=[0],
        data_types=[0],
        window_seconds=60,
        stride_seconds=10,
        max_rows=5000,
        near_seizure="none",
        exclude_near_seizure_seconds=3600,
        min_windows_per_patient=0,
        max_windows_per_patient=0,
        layout="TCH",
        verbose=False,
    )
    assert len(df_excluded) <= len(df_base)
