from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import re
from typing import Any

import geopandas as gpd
import pandas as pd

from ..sources import load_source_registry
from .surface import apply_surface_corrections, read_validated_corrections


GWPT_COLUMN_ALIASES = {
    "country_area": ("Country/Area",),
    "capacity_mw": ("Capacity (MW)",),
    "installation_type_source": ("Installation Type",),
    "status_source": ("Status",),
    "latitude": ("Latitude",),
    "longitude": ("Longitude",),
    "phase_id": ("GEM phase ID",),
}

GWPT_TEXT_COLUMNS = (
    "country_area",
    "installation_type_source",
    "status_source",
    "phase_id",
)

def _norm_header(value):
    return re.sub(r"[^a-z0-9]+", "", str(value).strip().lower())


def _find_col(columns, aliases):
    lookup = {_norm_header(column): str(column) for column in columns}
    for alias in aliases:
        key = _norm_header(alias)
        if key in lookup:
            return lookup[key]
    raise KeyError(f"Missing GWPT column; accepted aliases={aliases}")


def _normalise_text(series):
    return series.astype("string").str.strip().str.lower().str.replace(r"\s+", " ", regex=True)


def _surface_type(source, config):
    raw = source.astype("string").str.strip().str.lower()
    rule = config["surface_type"]
    onshore = {str(value).lower() for value in rule["onshore_exact"]}
    offshore = tuple(str(value).lower() for value in rule["offshore_prefixes"])
    result = pd.Series(str(rule["otherwise"]), index=raw.index, dtype="string")
    result.loc[raw.isin(onshore)] = "onshore"
    if offshore:
        mask = pd.Series(False, index=raw.index)
        for prefix in offshore:
            mask |= raw.str.startswith(prefix, na=False)
        result.loc[mask] = "offshore"
    return result


def _load_gwpt_config(source_registry_path):
    registry = load_source_registry(source_registry_path)
    if "GWPT" not in registry:
        raise KeyError("GWPT is not defined in sources.yaml")
    return registry["GWPT"]


def normalise_gwpt(workbook: Path, source_config: dict[str, Any]) -> pd.DataFrame:
    schema = source_config["schema"]
    sheet = str(schema["primary_sheet"])
    raw = pd.read_excel(workbook, sheet_name=sheet)

    mapping = {name: _find_col(raw.columns, aliases) for name, aliases in GWPT_COLUMN_ALIASES.items()}
    frame = pd.DataFrame({name: raw[column] for name, column in mapping.items()})
    for column in GWPT_TEXT_COLUMNS:
        frame[column] = frame[column].astype("string")
    for column in ("capacity_mw", "latitude", "longitude"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")

    frame["status"] = _normalise_text(frame["status_source"])
    frame["surface_type_source"] = _surface_type(
        frame["installation_type_source"], source_config["normalization"]
    )

    groups = schema["status_groups"]
    operating = set(groups["operating"])
    pipeline = set(groups["pipeline"])
    frame["is_operating"] = frame["status"].isin(operating)
    frame["is_pipeline"] = frame["status"].isin(pipeline)
    frame["is_active_boundary"] = frame["is_operating"] | frame["is_pipeline"]

    frame["coordinate_valid"] = (
        frame["longitude"].between(-180, 180, inclusive="both")
        & frame["latitude"].between(-90, 90, inclusive="both")
    )

    allowed_statuses = set(schema["allowed_statuses"])
    if allowed_statuses:
        unexpected = sorted(set(frame["status"].dropna()) - allowed_statuses)
        if unexpected:
            raise ValueError(f"Unexpected GWPT status values: {unexpected}")

    allowed_install = set(schema["allowed_installation_types"])
    if allowed_install:
        observed_install = set(frame["installation_type_source"].dropna().astype(str).str.strip())
        unexpected = sorted(observed_install - allowed_install)
        if unexpected:
            raise ValueError(f"Unexpected GWPT installation types: {unexpected}")

    phase_text = frame["phase_id"].astype("string")
    if frame["phase_id"].isna().any() or phase_text.str.strip().eq("").fillna(True).any():
        raise ValueError("GWPT contains empty GEM phase IDs")
    if frame["phase_id"].duplicated().any():
        duplicate = sorted(frame.loc[frame["phase_id"].duplicated(keep=False), "phase_id"].astype(str).unique())
        raise ValueError(f"GWPT GEM phase ID is not unique: {duplicate[:10]}")
    if (~frame["coordinate_valid"]).any():
        raise ValueError(f"GWPT contains {int((~frame['coordinate_valid']).sum())} invalid coordinate rows")

    minimum_capacity = float(schema["primary_capacity_min_mw"])
    if frame["capacity_mw"].isna().any():
        raise ValueError("GWPT contains non-numeric capacity values in the primary Data sheet")
    if (frame["capacity_mw"] < minimum_capacity).any():
        raise ValueError(f"GWPT primary sheet contains capacity below {minimum_capacity:g} MW")

    return frame[[
        "country_area", "capacity_mw", "latitude", "longitude", "phase_id",
        "status", "installation_type_source", "surface_type_source", "is_operating", "is_pipeline", "is_active_boundary",
    ]]


def run_gwpt_ingest(
    *,
    data_root: Path,
    source_registry_path: Path,
    surface_corrections_path: Path,
    output_dir: Path,
) -> dict[str, Any]:
    source_config = _load_gwpt_config(source_registry_path)
    files = source_config["files"]
    if len(files) != 1:
        raise ValueError("GWPT sources.yaml entry must define exactly one primary workbook")
    workbook = (data_root / str(files[0])).resolve()
    if not workbook.exists():
        raise FileNotFoundError(workbook)

    raw_normalized = normalise_gwpt(workbook, source_config)
    corrections = read_validated_corrections(surface_corrections_path)
    frame, correction_log = apply_surface_corrections(raw_normalized, corrections)
    correction_log.to_csv(output_dir / "surface_correction_applications.csv", index=False)

    geometry = gpd.points_from_xy(frame["longitude"], frame["latitude"])
    downstream = frame.drop(columns=["latitude", "longitude"])
    geo = gpd.GeoDataFrame(downstream, geometry=geometry, crs="EPSG:4326")
    geo.to_parquet(output_dir / "gwpt.parquet", index=False)
    qc = {
        "stage": "01_ingest",
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "source": str(workbook),
        "source_sheet": str(source_config["schema"]["primary_sheet"]),
        "rows": int(len(frame)),
        "unique_phase_ids": int(frame["phase_id"].nunique(dropna=True)),
        "capacity_total_mw": float(frame["capacity_mw"].sum()),
        "operating_mw": float(frame.loc[frame["is_operating"], "capacity_mw"].sum()),
        "pipeline_mw": float(frame.loc[frame["is_pipeline"], "capacity_mw"].sum()),
        "surface_type_source_counts": {
            str(key): int(value) for key, value in frame["surface_type_source"].value_counts(dropna=False).items()
        },
        "surface_type_verified_counts": {
            str(key): int(value) for key, value in frame["surface_type_verified"].value_counts(dropna=False).items()
        },
        "approved_corrections_applied": int(len(correction_log)),
        "approved_corrections_active_mw": float(frame.loc[frame["surface_correction_applied"].astype(bool) & frame["is_active_boundary"].astype(bool), "capacity_mw"].sum()),
        "status": "PASS",
    }
    return qc
