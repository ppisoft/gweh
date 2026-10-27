#!/usr/bin/env python3
"""Compare a cleaned GWEH run with an independently accepted reference run."""
from __future__ import annotations

import argparse
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import xarray as xr

SCIENCE_TABLES = (
    "05_exclusive/active_project_ownership.csv",
    "06_metrics/region_metrics.csv",
    "06_metrics/market_components.csv",
    "07_scores/scores.csv",
    "08_catalogue/catalogue.csv",
    "08_catalogue/selection_trace.csv",
)
GEOMETRY_LAYERS = (
    "05_exclusive/exclusive_regions.parquet",
    "05_exclusive/exclusive_denominators.parquet",
)


def compare_run(reference: Path, candidate: Path) -> list[str]:
    differences = []
    for relative in SCIENCE_TABLES:
        original = reference / relative
        revised = candidate / relative
        if not original.is_file() or not revised.is_file():
            differences.append(f"missing table {relative}")
        elif original.read_bytes() != revised.read_bytes():
            # Strict values/columns/row order; allow only equivalent empty cells.
            left = pd.read_csv(original, dtype=str).fillna("")
            right = pd.read_csv(revised, dtype=str).fillna("")
            try:
                pd.testing.assert_frame_equal(left, right, check_exact=True)
            except AssertionError:
                differences.append(f"CSV scientific values differ: {relative}")
    for relative in GEOMETRY_LAYERS:
        original = gpd.read_parquet(reference / relative).set_index("candidate_id")
        revised = gpd.read_parquet(candidate / relative).set_index("candidate_id")
        if set(original.index) != set(revised.index):
            differences.append(f"candidate universe differs in {relative}")
            continue
        for identifier in sorted(original.index):
            if original.loc[identifier].geometry.wkb != revised.loc[identifier].geometry.wkb:
                differences.append(f"WKB differs: {relative}: {identifier}")
        # All persisted scientific attributes are part of the reference schema.
        if set(original.columns) != set(revised.columns):
            differences.append(f"Stage05 attribute schema differs: {relative}")
            continue
        columns = sorted(set(original.columns) - {"geometry"})
        a = original.loc[sorted(original.index), columns]
        b = revised.loc[sorted(revised.index), columns]
        try:
            pd.testing.assert_frame_equal(a, b, check_dtype=False, check_exact=True)
        except AssertionError:
            differences.append(f"Stage05 attributes differ: {relative}")
    return differences


def compare_grid(reference: Path, candidate: Path) -> list[str]:
    with xr.open_dataset(reference) as source, xr.open_dataset(candidate) as revised:
        differences = []
        if set(source.variables) != set(revised.variables):
            differences.append("CF grid variable set differs")
        for name in sorted(set(source.variables) & set(revised.variables)):
            if source[name].dims != revised[name].dims:
                differences.append(f"CF variable dimensions differ: {name}")
            elif not np.array_equal(source[name].values, revised[name].values, equal_nan=True):
                differences.append(f"CF values differ: {name}")
        return differences


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reference-run", required=True, type=Path)
    parser.add_argument("--candidate-run", required=True, type=Path)
    parser.add_argument("--reference-grid", type=Path)
    parser.add_argument("--candidate-grid", type=Path)
    args = parser.parse_args()
    issues = compare_run(args.reference_run, args.candidate_run)
    if args.reference_grid or args.candidate_grid:
        if args.reference_grid is None or args.candidate_grid is None:
            parser.error("Specify both grid files to compare CF products")
        issues.extend(compare_grid(args.reference_grid, args.candidate_grid))
    if issues:
        for issue in issues:
            print(f"[DIFF] {issue}")
        raise SystemExit(2)
    print("[PASS] numerical tables and Stage05 WKB/attributes match reference")
    if args.reference_grid:
        print("[PASS] CF grid coordinate/data values match reference")


if __name__ == "__main__":
    main()
