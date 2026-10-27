#!/usr/bin/env python3
"""Independent point-location corroboration for GOWIRES, GRW and DeepOWT.

This is downstream scientific evidence, not a GWEH selection rule. Counts are
conditional on the relevant selected candidate denominators, not on the globe.
"""
from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path

import geopandas as gpd
import pandas as pd
from pyproj import Geod


def _points(path: Path, kind: str, *, ground_truth=False):
    if kind == "GOWIRES":
        frame = pd.read_csv(path, sep=";", encoding="cp1252", low_memory=False)
        columns = {name.lower(): name for name in frame.columns}
        lon_key = columns.get("longitude")
        lat_key = columns.get("latitude")
        if not lon_key or not lat_key:
            raise ValueError("GOWIRES must contain longitude/latitude fields")
        lon = pd.to_numeric(frame[lon_key], errors="coerce")
        lat = pd.to_numeric(frame[lat_key], errors="coerce")
        good = lon.between(-180, 180) & lat.between(-90, 90)
        return gpd.GeoDataFrame(geometry=gpd.points_from_xy(lon[good], lat[good]), crs="EPSG:4326"), len(frame)
    points = gpd.read_file(path)
    if points.crs is None:
        raise ValueError(f"Missing CRS in external {kind} file")
    points = points.to_crs("EPSG:4326")
    if ground_truth:
        # Calculate polygon centroids in local metric coordinates, not degrees.
        local_crs = points.estimate_utm_crs()
        if local_crs is None:
            raise ValueError(f"{kind}: cannot determine projected centroid CRS")
        points = points.copy()
        points["geometry"] = (
            points.to_crs(local_crs).geometry.centroid.to_crs("EPSG:4326")
        )
    if not points.geom_type.eq("Point").all():
        raise ValueError(f"{kind}: point geometry required (use --deepowt-gt for polygon ground truth)")
    valid = points.geometry.notna() & ~points.geometry.is_empty
    points = points.loc[valid].copy()
    return points[["geometry"]], len(valid)


def _unique_inside(points, regions, label):
    if points.empty:
        return pd.Series(dtype=str)
    # A selected candidate has a single spatial claim at a given coordinate.
    matches = gpd.sjoin(points, regions[["candidate_id", "geometry"]], how="inner", predicate="within")
    if matches.index.duplicated().any():
        raise RuntimeError(f"{label}: input point inside more than one region")
    return matches["candidate_id"].astype(str)


def _region_area_fractions(denominators: gpd.GeoDataFrame,
                           regions: gpd.GeoDataFrame) -> dict[str, float]:
    """Geodesic fraction of each denominator covered by its own region."""
    geod = Geod(ellps="WGS84")
    d = denominators.set_index("candidate_id").geometry
    r = regions.set_index("candidate_id").geometry
    if set(d.index) != set(r.index):
        raise ValueError("Area baseline needs matching region/denominator IDs")
    result = {}
    for candidate_id in d.index:
        denominator = d.loc[candidate_id]
        region = r.loc[candidate_id]
        # Region and denominator boundaries may differ. Only their overlap
        # is a meaningful denominator-scoped geometric reference area.
        overlap = region if region.within(denominator) else region.intersection(denominator)
        total = abs(geod.geometry_area_perimeter(denominator)[0])
        covered = abs(geod.geometry_area_perimeter(overlap)[0]) if not overlap.is_empty else 0.0
        if total <= 0:
            raise ValueError(f"{candidate_id}: denominator has zero geodesic area")
        result[str(candidate_id)] = max(0.0, min(1.0, covered / total))
    return result


def check_point_source(points: gpd.GeoDataFrame, denominator: gpd.GeoDataFrame,
                       selected: gpd.GeoDataFrame, source_name: str,
                       area_fractions: dict[str, float] | None = None):
    """One denominator -> candidate comparison; do not conflate missing with false."""
    points = points.reset_index(drop=True)
    scoped = _unique_inside(points, denominator, "denominators")
    assigned = _unique_inside(points, selected, "regions")
    rows = []
    for candidate_id in sorted(denominator.candidate_id.astype(str)):
        in_scope = scoped.eq(candidate_id)
        relevant = int(in_scope.sum())
        contained = int(((scoped == candidate_id) & (assigned == candidate_id)).sum())
        row = {
            "source": source_name, "candidate_id": candidate_id,
            "in_denominator": relevant, "in_same_selected_region": contained,
            "conditional_containment": contained / relevant if relevant else None,
        }
        if area_fractions is not None:
            row["region_area_fraction_of_denominator"] = area_fractions[candidate_id]
        rows.append(row)
    table = pd.DataFrame(rows)
    total_scoped = len(scoped)
    result = {
        "source": source_name,
        "valid_point_count": len(points),
        "within_selected_denominators": total_scoped,
        "inside_same_selected_region": int(table.in_same_selected_region.sum()),
        "conditional_containment": (
            float(table.in_same_selected_region.sum()) / total_scoped if total_scoped else None
        ),
        "outside_selected_denominators": int(len(points) - total_scoped),
        "scope": "Selected denominators for compatible onshore/offshore candidate surfaces",
    }
    if area_fractions is not None:
        baseline = (float((table.in_denominator * table.region_area_fraction_of_denominator).sum())
                    / total_scoped if total_scoped else None)
        result["scoped_region_area_fraction_point_weighted"] = baseline
        result["excess_containment_over_area_fraction"] = (
            result["conditional_containment"] - baseline if baseline is not None else None
        )
        result["area_baseline_method"] = "geodesic_area_of_region_intersection_denominator_weighted_by_scoped_points"
    return result, table


def run(run_dir: Path, *, gowires=None, grw=None, deepowt=None, deepowt_gt=(), output: Path):
    if output.exists():
        raise FileExistsError(output)
    # Fail immediately on an unwritable destination rather than after a long
    # spatial / source comparison has completed.
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".gweh_write_test_", dir=output.parent):
        pass
    catalog = pd.read_csv(run_dir / "08_catalogue/catalogue.csv", dtype={"candidate_id": str})
    chosen = set(catalog.loc[catalog.evidence_selection_status.eq("EVIDENCE_SELECTED"), "candidate_id"])
    regions = gpd.read_parquet(run_dir / "05_exclusive/exclusive_regions.parquet").to_crs("EPSG:4326")
    denominators = gpd.read_parquet(run_dir / "05_exclusive/exclusive_denominators.parquet").to_crs("EPSG:4326")
    if set(regions.candidate_id) != set(denominators.candidate_id):
        raise ValueError("Selected region and denominator ID populations differ")
    # Compute the geometric baseline once; the six sources reuse it.
    selected_ids = chosen & set(regions.candidate_id)
    all_fractions = _region_area_fractions(
        denominators.loc[denominators.candidate_id.isin(selected_ids)],
        regions.loc[regions.candidate_id.isin(selected_ids)],
    )
    sources = [("GOWIRES", gowires, False), ("GRW", grw, False),
               ("DeepOWT", deepowt, False)]
    sources.extend(("DeepOWT_ground_truth", p, True) for p in deepowt_gt)
    summaries, tables = [], []
    for label, path, gt in sources:
        if path is None:
            continue
        kind = "DeepOWT" if label.startswith("DeepOWT") else label
        points, raw_count = _points(Path(path), kind, ground_truth=gt)
        compatible = {"onshore", "mixed"} if kind != "DeepOWT" else {"offshore", "mixed"}
        eligible_ids = chosen & set(catalog.loc[catalog.surface_type.isin(compatible), "candidate_id"])
        den = denominators.loc[denominators.candidate_id.isin(eligible_ids)].copy()
        reg = regions.loc[regions.candidate_id.isin(eligible_ids)].copy()
        title = label if not gt else f"DeepOWT_GT_{Path(path).stem}"
        record, rows = check_point_source(points, den, reg, title, all_fractions)
        record["input_feature_count"] = raw_count
        summaries.append(record)
        tables.append(rows)
    if not summaries:
        raise ValueError("Provide at least one independent source dataset")
    output.mkdir(parents=True)
    pd.concat(tables, ignore_index=True).to_csv(output / "V1_V2_candidate_containment.csv", index=False)
    (output / "V1_V2_summary.json").write_text(
        json.dumps({"status": "COMPUTED", "population_candidate_count": len(catalog),
                    "sources": summaries, "limitations": [
                        "Containment is conditional on selected evidence denominators.",
                        "These observations are not the complete set of global wind installations.",
                        "No candidate membership is changed as a result of this comparison.",
                    ]}, indent=2) + "\n", encoding="utf-8")
    return summaries


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--run-dir", type=Path, required=True)
    p.add_argument("--gowires", type=Path)
    p.add_argument("--grw", type=Path)
    p.add_argument("--deepowt", type=Path)
    p.add_argument("--deepowt-gt", type=Path, nargs="*", default=[])
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args()
    print(json.dumps(run(args.run_dir, gowires=args.gowires, grw=args.grw,
                             deepowt=args.deepowt, deepowt_gt=args.deepowt_gt,
                             output=args.output), indent=2))


if __name__ == "__main__":
    main()
