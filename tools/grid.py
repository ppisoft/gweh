#!/usr/bin/env python3
"""Classify native GWEH regions on the supplied model-grid cell centres (CF-1.11)."""
from __future__ import annotations

import argparse
import tempfile
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import shapely
import xarray as xr
from shapely.strtree import STRtree

from cf_metadata import apply_metadata, refresh_file

MATURITY = {"CORE": 1, "EMERGING": 2, "PROSPECTIVE": 3}
SURFACE = {"onshore": 1, "offshore": 2, "mixed": 3}
TIERS = {"PRIMARY_CLIMATE_N_GE12": 1, "LOW_N_EXTENSION_8_11": 2, "NARROW_CLIMATE_2_7": 3}


def _coordinates(template: Path):
    """Read actual template coordinates; HDF5 fallback only for 1-D lat/lon."""
    try:
        with xr.open_dataset(template, decode_cf=False, mask_and_scale=False) as dataset:
            lat = np.asarray(dataset["lat"].values).copy()
            lon = np.asarray(dataset["lon"].values).copy()
            lat_attrs = dict(dataset["lat"].attrs)
            lon_attrs = dict(dataset["lon"].attrs)
    except (ValueError, OSError, ImportError):
        import h5py
        with h5py.File(template, "r") as handle:
            lat = handle["lat"][:]
            lon = handle["lon"][:]
            lat_attrs = {"units": "degrees_north"}
            lon_attrs = {"units": "degrees_east"}
    if lat.ndim != 1 or lon.ndim != 1 or min(len(lat), len(lon)) < 2:
        raise ValueError("Grid template requires one-dimensional latitude and longitude coordinates")
    if not np.isfinite(lat).all() or not np.isfinite(lon).all():
        raise ValueError("Non-finite grid-template coordinates")
    if not (np.diff(lat) > 0).all() or not (np.diff(lon) > 0).all():
        raise ValueError("Grid-template coordinates must be ascending")
    lat_attrs.pop("_FillValue", None)
    lon_attrs.pop("_FillValue", None)
    lat_attrs.update({"units": "degrees_north", "standard_name": "latitude", "axis": "Y"})
    lon_attrs.update({"units": "degrees_east", "standard_name": "longitude", "axis": "X"})
    return lat, lon, lat_attrs, lon_attrs


def _native_geometry(regions: gpd.GeoDataFrame):
    if regions.crs is None or regions.crs.to_epsg() != 4326:
        raise ValueError("Expected native EPSG:4326 geometries")
    if regions.candidate_id.duplicated().any():
        raise ValueError("Duplicate candidate_id")
    if regions.geometry.isna().any() or regions.geometry.is_empty.any() or not regions.is_valid.all():
        raise ValueError("Missing or invalid regional geometry; never repair silently")
    if not regions.geom_type.isin(["Polygon", "MultiPolygon"]).all():
        raise ValueError("Only polygonal GWEH geometries supported")


def classify_centres(regions: gpd.GeoDataFrame, lat, lon):
    """Strict point-in-polygon; a shared boundary is never assigned twice."""
    normalized_lon = ((np.asarray(lon) + 180.0) % 360.0) - 180.0
    xx, yy = np.meshgrid(normalized_lon, lat)
    points = shapely.points(xx.ravel(), yy.ravel())
    tree = STRtree(list(regions.geometry))
    point_index, geometry_index = tree.query(points, predicate="within")
    count = np.bincount(point_index, minlength=points.size)
    if np.any(count > 1):
        raise RuntimeError("Overlapping region interiors at CESM cell centres")
    assigned = np.zeros(points.size, dtype=np.int16)
    assigned[point_index] = geometry_index.astype(np.int16) + 1
    return assigned.reshape((len(lat), len(lon)))


def _tier(count: int) -> str:
    if count >= 12:
        return "PRIMARY_CLIMATE_N_GE12"
    if count >= 8:
        return "LOW_N_EXTENSION_8_11"
    if count >= 2:
        return "NARROW_CLIMATE_2_7"
    return "EXCLUDED_CLIMATE_SINGLE_CELL" if count == 1 else "EXCLUDED_CLIMATE_UNREPRESENTED"


def make_grid(run: Path, template: Path, output: Path, metadata_file: Path | None = None):
    """Create grid and complete candidate roster without fixed population counts."""
    if output.exists():
        raise FileExistsError(f"Output already exists: {output}")
    catalogue = pd.read_csv(run / "08_catalogue/catalogue.csv", dtype={"candidate_id": str})
    if catalogue.candidate_id.duplicated().any():
        raise ValueError("Duplicate catalogue IDs")
    regions = gpd.read_parquet(run / "05_exclusive/exclusive_regions.parquet")
    _native_geometry(regions)
    if set(regions.candidate_id.astype(str)) != set(catalogue.candidate_id):
        raise ValueError("Stage05 and catalogue candidate IDs differ")
    lat, lon, lat_attrs, lon_attrs = _coordinates(template)
    selected_ids = sorted(catalogue.loc[
        catalogue.evidence_selection_status.eq("EVIDENCE_SELECTED"), "candidate_id"
    ])
    selected = regions.set_index("candidate_id").loc[selected_ids].copy()
    raw_assignment = classify_centres(selected, lat, lon)
    counts = np.bincount(raw_assignment.ravel(), minlength=len(selected_ids) + 1)
    eligible_ids = [cid for index, cid in enumerate(selected_ids, 1) if counts[index] >= 2]
    encoded = np.zeros_like(raw_assignment)
    lookup_rows = []
    by_candidate = catalogue.set_index("candidate_id")
    for index, candidate_id in enumerate(eligible_ids, 1):
        source_index = selected_ids.index(candidate_id) + 1
        encoded[raw_assignment == source_index] = index
        item = by_candidate.loc[candidate_id]
        n = int(counts[source_index])
        lookup_rows.append({
            "grid_id": index, "candidate_id": candidate_id,
            "region_name": str(item.region_name), "surface_type": str(item.surface_type),
            "market_maturity": str(item.market_maturity),
            "analysis_use_tier": _tier(n), "cell_count": n,
        })

    n_center = dict(zip(selected_ids, (int(v) for v in counts[1:])))
    roster_rows = []
    for row in catalogue.itertuples(index=False):
        selected_flag = row.evidence_selection_status == "EVIDENCE_SELECTED"
        n = n_center.get(row.candidate_id) if selected_flag else None
        eligible = bool(selected_flag and n is not None and n >= 2)
        roster_rows.append({
            "candidate_id": row.candidate_id,
            "climate_grid_center_count": n if n is not None else "",
            "climate_analysis_eligible": int(eligible),
            "primary_climate_panel": int(eligible and n >= 12),
            "climate_use_tier": _tier(n) if selected_flag else "NOT_SELECTED",
        })

    # Separate the numerical classification from the spatial source identity.
    surface_codes = np.array([0] + [SURFACE[by_candidate.loc[cid].surface_type] for cid in eligible_ids], dtype=np.int8)
    maturity_codes = np.array([0] + [MATURITY[by_candidate.loc[cid].market_maturity] for cid in eligible_ids], dtype=np.int8)
    tier_codes = np.array([0] + [TIERS[_tier(n_center[cid])] for cid in eligible_ids], dtype=np.int8)
    flags = "outside_gweh " + " ".join(eligible_ids)
    data = xr.Dataset(coords={
        "lat": ("lat", lat, lat_attrs),
        "lon": ("lon", lon, lon_attrs),
        "region": ("region", np.arange(1, len(eligible_ids) + 1, dtype=np.int16),
                   {"long_name": "GWEH region index; see grid-region lookup"}),
    })
    data["gweh_region_id"] = xr.DataArray(
        encoded, dims=("lat", "lon"),
        attrs={"long_name": "GWEH exclusive region identifier", "units": "1",
               "flag_values": np.arange(len(eligible_ids) + 1, dtype=np.int16),
               "flag_meanings": flags},
    )
    data["gweh_region_mask"] = xr.DataArray(
        (encoded > 0).astype(np.int8), dims=("lat", "lon"),
        attrs={"long_name": "GWEH climate-region mask", "units": "1",
               "flag_values": np.array([0, 1], dtype=np.int8), "flag_meanings": "outside inside"},
    )
    data["gweh_region_surface_class"] = xr.DataArray(
        surface_codes[encoded], dims=("lat", "lon"),
        attrs={"long_name": "GWEH candidate surface class", "units": "1",
               "flag_values": np.array([0, 1, 2, 3], dtype=np.int8),
               "flag_meanings": "outside onshore offshore mixed"},
    )
    data["gweh_analysis_tier"] = xr.DataArray(
        tier_codes[encoded], dims=("lat", "lon"),
        attrs={"long_name": "Analysis tier based on grid-cell count", "units": "1",
               "flag_values": np.array([0, 1, 2, 3], dtype=np.int8),
               "flag_meanings": "outside primary_climate low_n_extension narrow_hotspot"},
    )
    data["gweh_market_maturity"] = xr.DataArray(
        maturity_codes[encoded], dims=("lat", "lon"),
        attrs={"long_name": "GWEH market maturity class", "units": "1",
               "flag_values": np.array([0, 1, 2, 3], dtype=np.int8),
               "flag_meanings": "outside core emerging prospective"},
    )
    data["gweh_region_cell_count"] = xr.DataArray(
        np.array([n_center[cid] for cid in eligible_ids], dtype=np.int32),
        dims=("region",),
        attrs={"long_name": "Number of grid-cell centres in region", "units": "1"},
    )
    data.attrs.update({
        "Conventions": "CF-1.11",
        "source": "GWEH Stage05 native EPSG:4326 exclusive regions",
        "grid_assignment_method": "strict point-in-polygon for template grid-cell centres",
        "grid_boundary_policy": "unassigned if exactly on a polygon boundary",
        "grid_single_cell_policy": "selected candidates with fewer than two centres excluded from mask",
    })
    if metadata_file is not None:
        apply_metadata(data, run=run, metadata_file=metadata_file)

    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".gweh_grid_", dir=output.parent) as temporary:
        stage = Path(temporary)
        nc = stage / f"GWEH_grid_{template.stem}.nc"
        data.to_netcdf(nc, engine="scipy", format="NETCDF3_64BIT", encoding={
            "lat": {"_FillValue": None}, "lon": {"_FillValue": None},
            "region": {"_FillValue": None},
        })
        pd.DataFrame(lookup_rows).to_csv(stage / "GWEH_grid_region_lookup.csv", index=False)
        pd.DataFrame(roster_rows).to_csv(stage / "GWEH_grid_candidate_roster.csv", index=False)
        # This is a descriptive summary, not a substitute for independent CF QA.
        (stage / "GRID_SUMMARY.txt").write_text(
            f"candidates={len(catalogue)}\nselected={len(selected_ids)}\n"
            f"climate_regions={len(eligible_ids)}\nassigned_cells={int(np.sum(encoded > 0))}\n"
        )
        stage.rename(output)
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--template", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--metadata", type=Path,
                        default=Path(__file__).resolve().parents[1] / "metadata/publication.yaml")
    parser.add_argument("--refresh-metadata", type=Path,
                        help="Existing validated NetCDF; update global metadata without regridding")
    args = parser.parse_args()
    if args.refresh_metadata is not None:
        target = refresh_file(args.refresh_metadata, args.output, args.run_dir, args.metadata)
        print(f"[done] CF metadata-only update: {target}")
    else:
        if args.template is None:
            parser.error("--template is required for grid construction")
        target = make_grid(args.run_dir, args.template, args.output, args.metadata)
        print(f"[done] grid candidate: {target}")


if __name__ == "__main__":
    main()
