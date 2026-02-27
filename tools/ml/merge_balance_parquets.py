#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import sys
from collections import Counter
from typing import Tuple

import numpy as np
import pandas as pd


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Merge preictal/interictal Parquets and balance classes deterministically."
    )
    parser.add_argument("--preictal", required=True, help="Input parquet with seizure_state=2 rows.")
    parser.add_argument("--interictal", required=True, help="Input parquet with seizure_state=0 rows.")
    parser.add_argument("--out", required=True, help="Output balanced parquet path.")
    parser.add_argument("--seed", type=int, default=42, help="Random seed for deterministic sampling.")
    parser.add_argument(
        "--strategy",
        choices=["downsample", "upsample"],
        default="downsample",
        help="Class balancing strategy.",
    )
    parser.add_argument(
        "--max-per-class",
        type=int,
        default=0,
        help="Optional cap per class after balancing (0 means no cap).",
    )
    return parser.parse_args()


def _load_non_empty_parquet(path: str, label: str) -> pd.DataFrame:
    if not os.path.exists(path):
        raise FileNotFoundError(f"{label} parquet not found: {path}")

    df = pd.read_parquet(path)
    if df.empty:
        raise ValueError(
            f"{label} parquet has 0 rows: {path}. "
            "Re-run export with a larger range or different patient."
        )
    if "seizure_state" not in df.columns:
        raise ValueError(f"{label} parquet is missing required column 'seizure_state': {path}")
    return df


def _validate_states(df_pre: pd.DataFrame, df_int: pd.DataFrame) -> None:
    pre_states = {int(v) for v in df_pre["seizure_state"].dropna().unique().tolist()}
    int_states = {int(v) for v in df_int["seizure_state"].dropna().unique().tolist()}

    if pre_states != {2}:
        raise ValueError(f"Preictal parquet must contain only seizure_state=2, found {sorted(pre_states)}")
    if int_states != {0}:
        raise ValueError(f"Interictal parquet must contain only seizure_state=0, found {sorted(int_states)}")


def _sample_to_n(df: pd.DataFrame, n: int, *, replace: bool, seed: int) -> pd.DataFrame:
    if n <= 0:
        raise ValueError("Target rows per class must be > 0 after balancing.")
    if len(df) == n and not replace:
        return df.copy()
    return df.sample(n=n, replace=replace, random_state=seed)


def _compute_target_size(
    n_pre: int, n_int: int, strategy: str, max_per_class: int
) -> Tuple[int, bool]:
    if strategy == "downsample":
        target = min(n_pre, n_int)
        replace = False
    elif strategy == "upsample":
        target = max(n_pre, n_int)
        replace = True
    else:
        raise ValueError(f"Unsupported strategy: {strategy}")

    if max_per_class > 0:
        target = min(target, max_per_class)
    return int(target), replace


def main() -> int:
    args = parse_args()

    df_pre = _load_non_empty_parquet(args.preictal, "Preictal")
    df_int = _load_non_empty_parquet(args.interictal, "Interictal")
    _validate_states(df_pre, df_int)

    target, replace = _compute_target_size(
        len(df_pre), len(df_int), args.strategy, int(args.max_per_class)
    )

    balanced_pre = _sample_to_n(df_pre, target, replace=(replace and len(df_pre) < target), seed=args.seed)
    balanced_int = _sample_to_n(df_int, target, replace=(replace and len(df_int) < target), seed=args.seed + 1)

    df_balanced = pd.concat([balanced_pre, balanced_int], ignore_index=True)
    df_balanced = df_balanced.sample(frac=1.0, random_state=args.seed).reset_index(drop=True)

    out_dir = os.path.dirname(args.out)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)

    df_balanced.to_parquet(args.out, index=False)

    counts = Counter(int(v) for v in df_balanced["seizure_state"].tolist())
    x_shape = np.asarray(df_balanced.iloc[0]["X"]).shape

    print(f"[OK] Wrote balanced parquet: {args.out}")
    print(f"[INFO] strategy={args.strategy}, seed={args.seed}, rows={len(df_balanced)}")
    for state in sorted(counts):
        print(f"class {state}: {counts[state]} rows")
    print(f"X[0] shape: {x_shape}")

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # noqa: BLE001
        print(f"[ERROR] {exc}", file=sys.stderr)
        raise SystemExit(1)
