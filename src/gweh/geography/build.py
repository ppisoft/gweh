from __future__ import annotations

from pathlib import Path
from typing import Any

import geopandas as gpd
from shapely.ops import unary_union

from .common import add_admin1_match_keys, add_match_keys, clean_geometry, clean_polygonal
from ..sources import load_source_registry, resolve_artifacts


_ADMIN_KEEP = (
    "ADMIN",
    "NAME",
    "NAME_LONG",
    "SOVEREIGNT",
    "BRK_NAME",
    "FORMAL_EN",
)

_EEZ_KEEP = (
    "TERRITORY1",
    "SOVEREIGN1",
    "TERRITORY2",
    "SOVEREIGN2",
    "TERRITORY3",
    "SOVEREIGN3",
    "GEONAME",
    "ISO_TER1",
    "ISO_SOV1",
    "ISO_TER2",
    "ISO_SOV2",
)

_ADMIN1_KEEP = (
    "name_en", "name", "nameascii", "gn_name", "woe_name", "postal", "iso_3166_2",
    # Preserve Natural Earth grouping attributes. Some countries are stored
    # at a finer polygon level while `region` carries the first-order unit
    # needed by GWEH (notably the Italian regions).
    "region", "region_big", "region_cod", "region_sub",
    "admin", "geonunit", "adm0_name", "sovereignt",
    "NAME_EN", "NAME", "NAMEASCII", "GN_NAME", "WOE_NAME", "POSTAL", "ISO_3166_2",
    "REGION", "REGION_BIG", "REGION_COD", "REGION_SUB",
    "ADMIN", "GEONUNIT", "ADM0_NAME", "SOVEREIGNT",
)


def _artifact_path(artifacts, dataset_id, suffix):
    matches = [item.path for item in artifacts if item.dataset_id == dataset_id and item.path.name.endswith(suffix)]
    if len(matches) != 1:
        raise RuntimeError(f"Expected one {dataset_id} artifact ending with {suffix}, found {len(matches)}")
    if not matches[0].exists():
        raise FileNotFoundError(matches[0])
    return matches[0]


def _load_vector(path):
    frame = gpd.read_file(path)
    if frame.crs is None:
        raise ValueError(f"Vector source has no CRS: {path}")
    frame = frame.to_crs("EPSG:4326")
    return frame


def _repair_polygon_frame(frame):
    invalid_before = int((~frame.geometry.is_valid).sum())
    out = frame.copy()
    out["geometry"] = out.geometry.map(clean_polygonal)
    empty = out.geometry.isna() | out.geometry.is_empty
    dropped = int(empty.sum())
    out = out.loc[~empty].copy()
    invalid_after = int((~out.geometry.is_valid).sum())
    if invalid_after:
        raise RuntimeError(f"Polygon normalization left {invalid_after} invalid geometries")
    return out, {
        "features_input": int(len(frame)),
        "features_output": int(len(out)),
        "invalid_before": invalid_before,
        "dropped_nonpolygonal_or_empty": dropped,
        "invalid_after": invalid_after,
    }


def _repair_line_frame(frame):
    invalid_before = int((~frame.geometry.is_valid).sum())
    out = frame.copy()
    out["geometry"] = out.geometry.map(clean_geometry)
    empty = out.geometry.isna() | out.geometry.is_empty
    dropped = int(empty.sum())
    out = out.loc[~empty].copy()
    invalid_after = int((~out.geometry.is_valid).sum())
    if invalid_after:
        raise RuntimeError(f"Line normalization left {invalid_after} invalid geometries")
    return out, {
        "features_input": int(len(frame)),
        "features_output": int(len(out)),
        "invalid_before": invalid_before,
        "dropped_empty": dropped,
        "invalid_after": invalid_after,
    }


def _slim(frame, keep):
    lookup = {str(column).casefold(): str(column) for column in frame.columns}
    columns = []
    for column in keep:
        actual = lookup.get(str(column).casefold())
        if actual and actual not in columns:
            columns.append(actual)
    columns.append("geometry")
    return frame[columns].copy()


def run_geography_build(
    *,
    data_root: Path,
    source_registry_path: Path,
    boundary: dict[str, str],
    output_dir: Path,
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    artifacts = resolve_artifacts(load_source_registry(source_registry_path), data_root)
    paths = {
        "land": _artifact_path(artifacts, boundary["physical_dataset_id"], boundary["land_suffix"]),
        "ocean": _artifact_path(artifacts, boundary["physical_dataset_id"], boundary["ocean_suffix"]),
        "coastline": _artifact_path(artifacts, boundary["physical_dataset_id"], boundary["coastline_suffix"]),
        "admin0": _artifact_path(artifacts, boundary["admin_dataset_id"], boundary["admin_suffix"]),
        "admin1": _artifact_path(artifacts, boundary["admin1_dataset_id"], boundary["admin1_suffix"]),
        "eez": _artifact_path(artifacts, boundary["eez_dataset_id"], boundary["eez_suffix"]),
    }

    raw_land = _load_vector(paths["land"])
    raw_ocean = _load_vector(paths["ocean"])
    raw_coast = _load_vector(paths["coastline"])
    raw_admin = _load_vector(paths["admin0"])
    raw_admin1 = _load_vector(paths["admin1"])
    raw_eez = _load_vector(paths["eez"])

    land_parts, land_qc = _repair_polygon_frame(raw_land)
    ocean_parts, ocean_qc = _repair_polygon_frame(raw_ocean)
    coast_parts, coast_qc = _repair_line_frame(raw_coast)
    admin, admin_qc = _repair_polygon_frame(_slim(raw_admin, _ADMIN_KEEP))
    admin1, admin1_qc = _repair_polygon_frame(_slim(raw_admin1, _ADMIN1_KEEP))
    eez, eez_qc = _repair_polygon_frame(_slim(raw_eez, _EEZ_KEEP))

    admin = add_match_keys(admin, "admin0")
    admin1 = add_admin1_match_keys(admin1)
    eez = add_match_keys(eez, "eez")

    land_geom = clean_polygonal(unary_union(land_parts.geometry.tolist()))
    ocean_geom = clean_polygonal(unary_union(ocean_parts.geometry.tolist()))
    coast_geom = clean_geometry(unary_union(coast_parts.geometry.tolist()))
    if land_geom is None or ocean_geom is None or coast_geom is None:
        raise RuntimeError("Failed to construct normalized global geography")

    land = gpd.GeoDataFrame([{"layer_id": "land", "geometry": land_geom}], geometry="geometry", crs="EPSG:4326")
    ocean = gpd.GeoDataFrame([{"layer_id": "ocean", "geometry": ocean_geom}], geometry="geometry", crs="EPSG:4326")
    coastline = gpd.GeoDataFrame(
        [{"layer_id": "coastline", "geometry": coast_geom}], geometry="geometry", crs="EPSG:4326"
    )

    layers = {
        "land": land,
        "ocean": ocean,
        "coastline": coastline,
        "admin0": admin,
        "admin1": admin1,
        "eez": eez,
    }

    for name, frame in layers.items():
        frame.to_parquet(output_dir / f"{name}.parquet", index=False)

    qc_by_layer = {
        "land": land_qc,
        "ocean": ocean_qc,
        "coastline": coast_qc,
        "admin0": admin_qc,
        "admin1": admin1_qc,
        "eez": eez_qc,
    }
    qc = {
        "status": "PASS",
        "crs": "EPSG:4326",
        "boundary_profile": boundary["name"],
        "physical_boundary_dataset_id": boundary["physical_dataset_id"],
        "layers": qc_by_layer,
        "admin0_match_key_rows": int((admin["match_keys"].str.len() > 0).sum()),
        "admin1_match_key_rows": int((admin1["admin1_match_keys"].str.len() > 0).sum()),
        "admin1_parent_key_rows": int((admin1["admin0_match_keys"].str.len() > 0).sum()),
        "eez_match_key_rows": int((eez["match_keys"].str.len() > 0).sum()),
    }
    return qc
