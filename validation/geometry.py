"""WKB identity of frozen blind V4 geometries and the public reference regions."""
from __future__ import annotations

import hashlib
from pathlib import Path

import geopandas as gpd
import pandas as pd

FINGERPRINT_FIELDS = ["validation_id", "wkb_sha256"]


def _read_mapping(path: Path) -> pd.DataFrame:
    mapping = pd.read_csv(path, dtype=str)
    if (list(mapping.columns) != ["candidate_id", "validation_id"]
            or mapping.isna().any().any()
            or mapping.candidate_id.duplicated().any()
            or mapping.validation_id.duplicated().any()
            or mapping.empty):
        raise ValueError("MERRA supervisor mapping must have unique candidate_id and validation_id")
    return mapping


def _fingerprints(frame: gpd.GeoDataFrame, key: str) -> pd.DataFrame:
    if (frame.crs is None or frame.crs.to_epsg() != 4326
            or key not in frame.columns or frame[key].isna().any()
            or frame[key].duplicated().any() or frame.empty
            or frame.geometry.isna().any() or frame.geometry.is_empty.any()
            or not frame.is_valid.all()
            or not frame.geom_type.isin(["Polygon", "MultiPolygon"]).all()):
        raise ValueError(f"Invalid V4 geometries or IDs for {key}")
    return pd.DataFrame({
        key: frame[key].astype(str).to_numpy(),
        "wkb_sha256": [hashlib.sha256(geometry.wkb).hexdigest()
                       for geometry in frame.geometry],
    })


def create_blind_fingerprints(blind_gpkg: Path, mapping_file: Path,
                              output: Path) -> int:
    """Record only each frozen WKB checksum, not the duplicate GeoPackage."""
    mapping = _read_mapping(mapping_file)
    blind = _fingerprints(gpd.read_file(blind_gpkg, layer="regions"), "validation_id")
    if set(mapping.validation_id) != set(blind.validation_id):
        raise ValueError("Blind MERRA geometry and sealed mapping have different IDs")
    blind = blind.sort_values("validation_id")
    output.parent.mkdir(parents=True, exist_ok=True)
    blind.to_csv(output, index=False)
    return len(blind)


def verify_public_fingerprints(public_gpkg: Path, mapping_file: Path,
                               fingerprints_file: Path) -> int:
    """Require exact WKB identity for every blind ID and its public candidate."""
    mapping = _read_mapping(mapping_file)
    fingerprints = pd.read_csv(fingerprints_file, dtype=str)
    if (list(fingerprints.columns) != FINGERPRINT_FIELDS
            or fingerprints.empty or fingerprints.isna().any().any()
            or fingerprints.validation_id.duplicated().any()
            or not fingerprints.wkb_sha256.str.fullmatch(r"[a-f0-9]{64}").all()):
        raise ValueError("Invalid blind geometry fingerprint table")
    public = _fingerprints(gpd.read_file(public_gpkg, layer="regions"), "candidate_id")
    if (set(mapping.validation_id) != set(fingerprints.validation_id)
            or set(mapping.candidate_id) != set(public.candidate_id)):
        raise ValueError("Incomplete V4 blind-to-public geometry mapping")
    linked = (mapping.merge(fingerprints, on="validation_id", validate="one_to_one")
             .merge(public, on="candidate_id", validate="one_to_one",
                    suffixes=("_blind", "_public")))
    if len(linked) != len(mapping) or not linked.wkb_sha256_blind.eq(linked.wkb_sha256_public).all():
        mismatches = linked.loc[~linked.wkb_sha256_blind.eq(linked.wkb_sha256_public),
                                "candidate_id"].tolist()
        raise ValueError(f"V4 blind geometry WKB differs from public regions: {mismatches}")
    return len(linked)
