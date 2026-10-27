#!/usr/bin/env python3
"""Read-only independent omission challenges for GWEH candidate geography.

Expert proposals from other literature are not automatically accepted candidates.
Their intersections with existing GWEH regions and denominators are reported.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
from scipy.spatial import cKDTree

from gweh.geography.common import geodesic_area_km2


def read_expert_proposals(path: Path):
    path = Path(path)
    if path.suffix.lower() == ".csv":
        rows = pd.read_csv(path, dtype={"proposal_id": str, "source_url": str})
        needed = {"proposal_id", "source_url", "longitude", "latitude"}
        if not needed <= set(rows.columns):
            raise ValueError(f"Expert CSV requires columns {sorted(needed)}")
        lon = pd.to_numeric(rows.longitude, errors="coerce")
        lat = pd.to_numeric(rows.latitude, errors="coerce")
        if not (lon.between(-180, 180) & lat.between(-90, 90)).all():
            raise ValueError("Expert proposal coordinates are invalid")
        frame = gpd.GeoDataFrame(rows, geometry=gpd.points_from_xy(lon, lat), crs="EPSG:4326")
    else:
        frame = gpd.read_file(path)
        if frame.crs is None:
            raise ValueError("Expert vector file has no coordinate reference system")
        frame = frame.to_crs("EPSG:4326")
        if not {"proposal_id", "source_url"} <= set(frame.columns):
            raise ValueError("Expert vector file needs proposal_id and source_url")
    if frame.proposal_id.astype(str).duplicated().any():
        raise ValueError("Repeated expert proposal ID")
    if frame.geometry.isna().any() or frame.geometry.is_empty.any() or not frame.is_valid.all():
        raise ValueError("Invalid expert proposal geometries")
    if not frame.geom_type.isin(["Point", "Polygon", "MultiPolygon"]).all():
        raise ValueError("Expert nominations must be points or polygons")
    return frame


def compare_expert_proposals(proposals, regions, denominators):
    """Assess overlap only; never modify candidate definitions or scores."""
    results = []
    for proposal in proposals.itertuples(index=False):
        geometry = proposal.geometry
        point = geometry.geom_type == "Point"
        region_ids = []
        denominator_ids = []
        fractions = []
        for table, found in ((regions, region_ids), (denominators, denominator_ids)):
            for row in table.itertuples(index=False):
                if point:
                    matched = geometry.within(row.geometry)
                else:
                    matched = geometry.intersects(row.geometry)
                    if matched and table is regions:
                        intersection = geometry.intersection(row.geometry)
                        fractions.append(geodesic_area_km2(intersection) / geodesic_area_km2(geometry))
                if matched:
                    found.append(str(row.candidate_id))
        results.append({
            "proposal_id": str(proposal.proposal_id),
            "source_url": str(proposal.source_url),
            "geometry_type": "point" if point else "polygon",
            "inside_or_intersecting_regions": ";".join(sorted(region_ids)),
            "inside_or_intersecting_denominators": ";".join(sorted(denominator_ids)),
            "maximum_fraction_in_one_existing_region": (
                float(max(fractions)) if fractions else (None if point else 0.0)
            ),
            "review_outside_existing_denominators": not bool(denominator_ids),
        })
    return pd.DataFrame(results)


def _unowned_clusters(run: Path, radius_km: float):
    """Descriptive GWPT unowned clusters; a cluster is NOT a proposed hotspot."""
    if not 0 < radius_km <= 250:
        raise ValueError("Omision screen distance must be within (0, 250] km")
    gwpt = gpd.read_parquet(run / "01_ingest/gwpt.parquet")
    owners = pd.read_csv(run / "05_exclusive/active_project_ownership.csv", dtype={"phase_id": str})
    if owners.phase_id.duplicated().any() or gwpt.phase_id.astype(str).duplicated().any():
        raise ValueError("Duplicate phase ID")
    active = gwpt.loc[gwpt.is_active_boundary.astype(bool)].copy()
    active["phase_id"] = active.phase_id.astype(str)
    active = active.merge(owners[["phase_id", "owner_candidate_id"]], on="phase_id", how="left", validate="one_to_one")
    pending = active.loc[active.owner_candidate_id.fillna("").astype(str).eq("")].copy()
    if pending.empty:
        return pd.DataFrame(columns=["cluster_id", "phase_count", "capacity_mw", "lat_min", "lat_max", "lon_min", "lon_max"])
    points = gpd.GeoDataFrame(pending, geometry="geometry", crs=gwpt.crs).to_crs("EPSG:4326")
    if not points.geom_type.eq("Point").all():
        raise ValueError("Unowned active phases have non-point geometry")
    lat = np.deg2rad(points.geometry.y.to_numpy())
    lon = np.deg2rad(points.geometry.x.to_numpy())
    coords = np.column_stack((np.cos(lat)*np.cos(lon), np.cos(lat)*np.sin(lon), np.sin(lat)))
    pairs = cKDTree(coords).query_pairs(2 * np.sin((radius_km/6371.0088)/2), output_type="ndarray")
    if len(pairs) > 3_000_000:
        raise RuntimeError("Omission proximity pairs exceed memory budget; use a smaller radius")
    parent = list(range(len(points)))
    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i
    for left, right in pairs:
        a, b = find(int(left)), find(int(right))
        if a != b:
            parent[b] = a
    roots = sorted({find(i) for i in range(len(points))})
    mapping = {r: i + 1 for i, r in enumerate(roots)}
    points["cluster_id"] = [mapping[find(i)] for i in range(len(points))]
    points["lat"] = points.geometry.y.to_numpy()
    points["lon"] = points.geometry.x.to_numpy()
    return points.groupby("cluster_id", sort=True).agg(
        phase_count=("phase_id", "size"), capacity_mw=("capacity_mw", "sum"),
        lat_min=("lat", "min"), lat_max=("lat", "max"),
        lon_min=("lon", "min"), lon_max=("lon", "max"),
    ).reset_index()


def screen(run: Path, proposals: Path | None, output: Path, radius_km: float | None = None):
    if output.exists():
        raise FileExistsError(output)
    regions = gpd.read_parquet(run / "05_exclusive/exclusive_regions.parquet").to_crs("EPSG:4326")
    denominators = gpd.read_parquet(run / "05_exclusive/exclusive_denominators.parquet").to_crs("EPSG:4326")
    if not proposals and radius_km is None:
        raise ValueError("Supply --expert-proposals, --unowned-radius-km, or both")
    output.mkdir(parents=True)
    result = {"status": "DESCRIPTIVE_REVIEW_ONLY", "candidate_count": len(regions),
              "no_changes_to_science": True,
              "scope": "Exploratory spatial omission challenge, not automatic nomination"}
    if proposals is not None:
        proposals_frame = read_expert_proposals(proposals)
        compared = compare_expert_proposals(proposals_frame, regions, denominators)
        compared.to_csv(output / "expert_omission_review.csv", index=False)
        result["expert_proposal_count"] = len(compared)
        result["outside_existing_denominators"] = int(compared.review_outside_existing_denominators.sum())
    if radius_km is not None:
        clusters = _unowned_clusters(run, radius_km)
        clusters.to_csv(output / "unowned_active_proximity_groups.csv", index=False)
        result["unowned_cluster_count"] = len(clusters)
        result["radius_km"] = radius_km
        result["single_linkage_distance_caveat"] = "Cluster chains may exceed the specified pairwise radius"
    (output / "OMISSION_REVIEW.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--run-dir", required=True, type=Path)
    p.add_argument("--expert-proposals", type=Path)
    p.add_argument("--unowned-radius-km", type=float)
    p.add_argument("--output", required=True, type=Path)
    a = p.parse_args()
    print(json.dumps(screen(a.run_dir, a.expert_proposals, a.output, a.unowned_radius_km), indent=2))


if __name__ == "__main__":
    main()
