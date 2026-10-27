from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import geopandas as gpd
import pandas as pd
import shapely
from shapely.geometry import Polygon

from ..geography.common import geodesic_area_km2, local_aeqd_crs, project_geometry
from .common import (
    polygonal,
    safe_difference,
    safe_intersection,
    union_polygonal,
)


def _core_seed(left, right):
    core = safe_difference(left, right, label="pair core")
    if core is not None and not core.is_empty:
        return core.representative_point()
    return left.representative_point()


def split_overlap_pair(left, right):
    overlap = safe_intersection(left, right, label="pair overlap")
    if overlap is None or overlap.is_empty:
        return None, None, None, None

    seed_left = _core_seed(left, right)
    seed_right = _core_seed(right, left)
    support = union_polygonal([left, right])
    local = local_aeqd_crs(support)
    overlap_local = polygonal(project_geometry(overlap, "EPSG:4326", local))
    if overlap_local is None or overlap_local.is_empty:
        # Extremely small/sliver overlaps can collapse during the local
        # projection/repair step. Preserve union and exclusivity with a
        # deterministic whole-overlap assignment rather than dereferencing
        # an absent projected polygon. Normal production overlaps never use
        # this branch.
        point = overlap.representative_point()
        if _point_distance_km(point, seed_left) <= _point_distance_km(point, seed_right):
            return overlap, None, seed_left, seed_right
        return None, overlap, seed_left, seed_right
    left_local = project_geometry(seed_left, "EPSG:4326", local)
    right_local = project_geometry(seed_right, "EPSG:4326", local)

    x1, y1 = float(left_local.x), float(left_local.y)
    x2, y2 = float(right_local.x), float(right_local.y)
    dx, dy = x1 - x2, y1 - y2
    distance = math.hypot(dx, dy)
    if distance < 1.0:
        a = project_geometry(left, "EPSG:4326", local).centroid
        b = project_geometry(right, "EPSG:4326", local).centroid
        x1, y1, x2, y2 = float(a.x), float(a.y), float(b.x), float(b.y)
        dx, dy = x1 - x2, y1 - y2
        distance = math.hypot(dx, dy)
    if distance < 1.0:
        dx, dy, distance = 1.0, 0.0, 1.0

    ux, uy = dx / distance, dy / distance
    nx, ny = -uy, ux
    mx, my = 0.5 * (x1 + x2), 0.5 * (y1 + y2)
    minx, miny, maxx, maxy = overlap_local.bounds
    span = max(maxx - minx, maxy - miny, distance, 10000.0)
    length = span * 20.0 + 10000.0
    half_plane = Polygon(
        [
            (mx + nx * length, my + ny * length),
            (mx - nx * length, my - ny * length),
            (mx - nx * length + ux * 2 * length, my - ny * length + uy * 2 * length),
            (mx + nx * length + ux * 2 * length, my + ny * length + uy * 2 * length),
        ]
    )
    keep_left_local = safe_intersection(overlap_local, half_plane, label="metric half-plane split")
    keep_right_local = safe_difference(
        overlap_local,
        keep_left_local,
        label="metric complementary half-plane",
    )
    keep_left = polygonal(project_geometry(keep_left_local, local, "EPSG:4326")) if keep_left_local is not None else None
    keep_right = polygonal(project_geometry(keep_right_local, local, "EPSG:4326")) if keep_right_local is not None else None
    return keep_left, keep_right, seed_left, seed_right


def pair_overlap_table(frame: gpd.GeoDataFrame, min_km2: float) -> pd.DataFrame:
    rows = []
    pairs = []
    try:
        seen = set()
        spatial_index = frame.sindex
        for i, geometry in enumerate(frame.geometry):
            if geometry is None or geometry.is_empty:
                continue
            for value in spatial_index.query(geometry, predicate="intersects"):
                j = int(value)
                if j > i:
                    seen.add((i, j))
        pairs = sorted(seen)
    except Exception:
        pairs = [(i, j) for i in range(len(frame)) for j in range(i + 1, len(frame))]

    for i, j in pairs:
        left = frame.iloc[i]
        right = frame.iloc[j]
        if not left.geometry.intersects(right.geometry):
            continue
        overlap = safe_intersection(left.geometry, right.geometry, label="overlap check")
        area = geodesic_area_km2(overlap) if overlap is not None else 0.0
        if area >= float(min_km2):
            rows.append(
                {
                    "candidate_id_1": str(left.candidate_id),
                    "candidate_id_2": str(right.candidate_id),
                    "surface_type_1": str(left.surface_type),
                    "surface_type_2": str(right.surface_type),
                    "overlap_area_km2": float(area),
                }
            )
    return pd.DataFrame(
        rows,
        columns=[
            "candidate_id_1", "candidate_id_2", "surface_type_1", "surface_type_2", "overlap_area_km2"
        ],
    )


def _repair_union_gaps(base, final, min_piece_km2=1.0e-8):
    """Conserve the union without reintroducing overlaps during gap allocation.

    Gap fragments are carved out of the *currently claimed* union before being
    given to an admissible candidate. Adding a gap directly to an existing
    polygon can leave positive-area intersections after robust GEOS overlays.
    """
    clipped = [safe_intersection(geometry, base[i], label="final-to-base clip") for i, geometry in enumerate(final)]
    union_base = union_polygonal(base)
    claimed = union_polygonal(clipped)
    gap = safe_difference(union_base, claimed, label="union preservation gap")
    if gap is None or gap.is_empty:
        return clipped

    for part in shapely.get_parts(gap):
        remainder = polygonal(part)
        guard = 0
        while remainder is not None and not remainder.is_empty and guard < len(base) + 2:
            guard += 1
            candidates = []
            for i, geometry in enumerate(base):
                if geometry is None or not geometry.intersects(remainder):
                    continue
                intersection = safe_intersection(geometry, remainder, label="remainder allocation")
                if intersection is None or intersection.is_empty:
                    continue
                available = safe_difference(intersection, claimed, label="unclaimed remainder")
                if available is None or available.is_empty:
                    continue
                area = geodesic_area_km2(available)
                if area > min_piece_km2:
                    candidates.append((area, -i, i, available))
            if not candidates:
                break
            # Deterministic tie-break by original candidate order.
            _, _, i, available = max(candidates)
            clipped[i] = union_polygonal([clipped[i], available])
            claimed = union_polygonal([claimed, available])
            remainder = safe_difference(remainder, available, label="allocated remainder subtraction")
    return clipped


def _point_distance_km(left, right):
    lon1, lat1, lon2, lat2 = map(math.radians, [float(left.x), float(left.y), float(right.x), float(right.y)])
    h = math.sin((lat2 - lat1) / 2.0) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2.0) ** 2
    return 6371.0088 * 2.0 * math.atan2(math.sqrt(h), math.sqrt(max(0.0, 1.0 - h)))


def _resolve_residual_overlaps(
    frame,
    base_geometries,
    *,
    trigger_km2,
    max_passes,
):
    work = frame.copy().sort_values("candidate_id", kind="mergesort").reset_index(drop=True)
    work["candidate_id"] = work["candidate_id"].astype(str)
    base_map = {str(candidate_id): polygonal(geometry) for candidate_id, geometry in zip(work["candidate_id"], base_geometries)}
    for _ in range(int(max_passes)):
        changed = False
        overlaps = pair_overlap_table(work, float(trigger_km2))
        if overlaps.empty:
            break
        index = {str(candidate_id): i for i, candidate_id in enumerate(work["candidate_id"])}
        for record in overlaps.itertuples(index=False):
            i = index[str(record.candidate_id_1)]
            j = index[str(record.candidate_id_2)]
            left = polygonal(work.at[i, "geometry"])
            right = polygonal(work.at[j, "geometry"])
            overlap = safe_intersection(left, right, label="residual overlap")
            area_before = geodesic_area_km2(overlap) if overlap is not None else 0.0
            if area_before <= float(trigger_km2):
                continue

            keep_left, _, seed_left, seed_right = split_overlap_pair(left, right)
            keep_left = safe_intersection(keep_left, overlap, label="residual keep-left clip") if keep_left is not None else None
            keep_right = safe_difference(overlap, keep_left, label="residual exact complement") if keep_left is not None else overlap
            left_core = safe_difference(left, overlap, label="residual left core")
            right_core = safe_difference(right, overlap, label="residual right core")
            left_new = union_polygonal([left_core, keep_left])
            right_new = union_polygonal([right_core, keep_right])

            left_id = str(work.at[i, "candidate_id"])
            right_id = str(work.at[j, "candidate_id"])
            left_new = safe_intersection(left_new, base_map[left_id], label="residual left base clip")
            right_new = safe_intersection(right_new, base_map[right_id], label="residual right base clip")

            duplicate = safe_intersection(left_new, right_new, label="residual duplicate")
            duplicate_area = geodesic_area_km2(duplicate) if duplicate is not None else 0.0
            if duplicate_area > 0.0:
                point = duplicate.representative_point()
                if _point_distance_km(point, seed_left) <= _point_distance_km(point, seed_right):
                    right_new = safe_difference(right_new, duplicate, label="duplicate remove right")
                else:
                    left_new = safe_difference(left_new, duplicate, label="duplicate remove left")

            if left_new is None or left_new.is_empty or right_new is None or right_new.is_empty:
                raise RuntimeError(f"Residual cleanup emptied candidate pair {left_id} / {right_id}")
            work.at[i, "geometry"] = polygonal(left_new)
            work.at[j, "geometry"] = polygonal(right_new)
            changed = True
        if not changed:
            break

    return gpd.GeoDataFrame(work, geometry="geometry", crs=frame.crs)


def partition_geometries(
    frame: gpd.GeoDataFrame,
    *,
    min_overlap_km2: float,
    residual_trigger_km2: float,
    residual_max_passes: int,
) -> gpd.GeoDataFrame:
    original_ids = frame["candidate_id"].astype(str).tolist()
    work = frame.copy()
    work["candidate_id"] = work["candidate_id"].astype(str)
    work = work.sort_values("candidate_id", kind="mergesort").reset_index(drop=True)
    base = [polygonal(geometry) for geometry in work.geometry]
    constraints = {}
    for i in range(len(base)):
        for j in range(i + 1, len(base)):
            if not base[i].intersects(base[j]):
                continue
            overlap = safe_intersection(base[i], base[j], label="partition overlap")
            area = geodesic_area_km2(overlap) if overlap is not None else 0.0
            if area < float(min_overlap_km2):
                continue
            keep_i, keep_j, _, _ = split_overlap_pair(base[i], base[j])
            if keep_i is None and keep_j is None:
                continue
            constraints[(i, j)] = keep_i
            constraints[(j, i)] = keep_j

    final = []
    for i in range(len(base)):
        geometry = base[i]
        for j in range(len(base)):
            if i == j or (i, j) not in constraints:
                continue
            overlap = safe_intersection(base[i], base[j], label="partition overlap")
            if overlap is None or overlap.is_empty:
                continue
            keep = constraints[(i, j)]
            loss = safe_difference(overlap, keep, label="pair overlap loss") if keep is not None else overlap
            if loss is not None and not loss.is_empty:
                geometry = safe_difference(geometry, loss, label="owner overlap removal")
            if geometry is None or geometry.is_empty:
                raise RuntimeError(f"Partition emptied candidate {work.iloc[i].candidate_id}")
        final.append(geometry)

    final = _repair_union_gaps(base, final)
    temporary = work.copy()
    temporary["geometry"] = final
    temporary = _resolve_residual_overlaps(
        gpd.GeoDataFrame(temporary, geometry="geometry", crs=frame.crs),
        base,
        trigger_km2=float(residual_trigger_km2),
        max_passes=int(residual_max_passes),
    )
    repaired = _repair_union_gaps(base, [polygonal(geometry) for geometry in temporary.geometry])
    temporary = temporary.copy()
    temporary["geometry"] = repaired
    # The final gap allocation must also be tested for overlap. It is not
    # enough for the earlier residual pass to have converged: overlay repair
    # may produce small positive-area fragments on complex shared boundaries.
    temporary = _resolve_residual_overlaps(
        gpd.GeoDataFrame(temporary, geometry="geometry", crs=frame.crs),
        base,
        trigger_km2=float(residual_trigger_km2),
        max_passes=int(residual_max_passes),
    )
    output = temporary.set_index("candidate_id").loc[original_ids].reset_index()
    return gpd.GeoDataFrame(output, geometry="geometry", crs=frame.crs)


def _read_persisted_parquet(path, expected_ids):
    """Read exactly what GeoParquet stores, without modifying the geometry.

    Applying make_valid/unary_union while reading would make QC examine a
    different geometry from the object actually serialized to disk.
    """
    frame = gpd.read_parquet(path).to_crs("EPSG:4326").reset_index(drop=True)
    if "candidate_id" not in frame.columns:
        raise RuntimeError(f"Persisted geometry lacks candidate_id: {path}")
    frame["candidate_id"] = frame["candidate_id"].astype(str)
    if frame["candidate_id"].duplicated().any():
        raise RuntimeError(f"Duplicate candidate_id after persistence: {path}")
    if set(frame["candidate_id"]) != set(expected_ids):
        raise RuntimeError(f"Candidate universe changed during persistence: {path}")
    frame = frame.set_index("candidate_id").loc[expected_ids].reset_index()
    if frame.geometry.isna().any() or frame.geometry.is_empty.any():
        raise RuntimeError(f"Empty geometry after persistence: {path}")
    if not bool(frame.geometry.is_valid.all()):
        bad = frame.loc[~frame.geometry.is_valid, "candidate_id"].tolist()
        raise RuntimeError(f"Invalid persisted geometries in {path}: {bad}")
    if not frame.geometry.geom_type.isin(["Polygon", "MultiPolygon"]).all():
        raise RuntimeError(f"Non-polygonal geometry persisted in {path}")
    return gpd.GeoDataFrame(frame, geometry="geometry", crs="EPSG:4326")


def _reconcile_persistence_artifacts(persisted, overlaps, *, max_artifact_km2, verify_tolerance_km2):
    """Remove only negligible duplicated slivers; use one acyclic owner order.

    The earlier local majority/seed choice could create inconsistent cycles for
    triple overlaps. The stable sorted candidate_id order makes each removal
    monotone and avoids returning a removed fragment to another candidate.
    Existing ownership of all non-overlapping area is untouched.
    """
    work = persisted.copy().sort_values("candidate_id", kind="mergesort").reset_index(drop=True)
    work["candidate_id"] = work["candidate_id"].astype(str)
    index = {str(cid): i for i, cid in enumerate(work["candidate_id"])}
    actions = 0
    cleanup_area = 0.0

    for record in overlaps.itertuples(index=False):
        left_id = str(record.candidate_id_1)
        right_id = str(record.candidate_id_2)
        winner = min(left_id, right_id)
        loser = max(left_id, right_id)
        i, j = index[winner], index[loser]
        left = work.at[i, "geometry"]
        right = work.at[j, "geometry"]
        if not left.intersects(right):
            continue
        overlap = safe_intersection(left, right, label="post-write residual overlap")
        area = geodesic_area_km2(overlap) if overlap is not None else 0.0
        if area < float(verify_tolerance_km2):
            continue
        if area > float(max_artifact_km2):
            raise RuntimeError(
                f"Positive overlap {winner}/{loser} is {area:.9f} km2, "
                f"above the permitted numerical-artifact limit {max_artifact_km2:.9f} km2; "
                "scientific geometry must be inspected (not automatically reassigned)."
            )
        # Difference against the WHOLE winner geometry, rather than against
        # a separately recomputed overlap polygon; the latter can leave a
        # positive-area residue at boundaries after GEOS/WKB round-tripping.
        remainder = safe_difference(right, left, label="post-write subtract owner")
        if remainder is None or remainder.is_empty:
            raise RuntimeError(f"Post-write cleanup would empty candidate {loser}")
        work.at[j, "geometry"] = remainder
        actions += 1
        cleanup_area += area
    return gpd.GeoDataFrame(work, geometry="geometry", crs=persisted.crs), actions, cleanup_area


def _overlap_details(path, overlaps, *, pass_number, cleanup_actions, cleanup_area_km2):
    """Leave a machine-readable diagnosis in the failed Stage-05 workspace."""
    diagnostic = path.parent / f"{path.stem}_topology_overlaps.csv"
    if overlaps is not None:
        table = overlaps.copy()
        table.insert(0, "verification_pass", int(pass_number))
        table.to_csv(diagnostic, index=False)
    summary = {
        "output": path.name,
        "verification_pass": int(pass_number),
        "overlap_pair_count": int(len(overlaps)),
        "overlap_area_km2_sum": float(overlaps["overlap_area_km2"].sum()) if not overlaps.empty else 0.0,
        "overlap_area_km2_max": float(overlaps["overlap_area_km2"].max()) if not overlaps.empty else 0.0,
        "cleanup_actions": int(cleanup_actions),
        "cleanup_area_km2": float(cleanup_area_km2),
        "overlap_csv": diagnostic.name,
    }
    (path.parent / f"{path.stem}_topology_diagnostic.json").write_text(
        __import__("json").dumps(summary, indent=2) + "\n", encoding="utf-8"
    )
    return diagnostic


def persist_topology_safe_parquet(
    frame: gpd.GeoDataFrame,
    path: Path,
    *,
    cleanup_trigger_km2: float,
    verify_tolerance_km2: float,
    max_artifact_km2: float,
    max_passes: int = 8,
) -> tuple[gpd.GeoDataFrame, dict[str, Any]]:
    """Persist, re-read and iteratively verify the actual on-disk topology.

    The verification always uses the strict *postwrite* threshold (not a more
    permissive cleanup trigger). It never weakens the tolerance to declare PASS.
    Every numerical correction must be smaller than max_artifact_km2, and the
    scientific union-conservation QC remains mandatory in Stage 05.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    expected_ids = frame["candidate_id"].astype(str).tolist()
    if len(expected_ids) != len(set(expected_ids)):
        raise RuntimeError("Duplicate IDs before geometry persistence")
    if int(max_passes) < 1:
        raise ValueError("max_passes must be at least 1")
    if not (0 < float(verify_tolerance_km2) <= float(cleanup_trigger_km2)):
        raise ValueError("verify_tolerance_km2 must be positive and <= cleanup_trigger_km2")

    frame.to_parquet(path, index=False)
    persisted = _read_persisted_parquet(path, expected_ids)
    initial_overlaps = pair_overlap_table(persisted, float(verify_tolerance_km2))
    initial_pairs = int(len(initial_overlaps))
    initial_area = float(initial_overlaps["overlap_area_km2"].sum()) if initial_pairs else 0.0
    cleanup_actions = 0
    cleanup_area = 0.0
    current = initial_overlaps
    checks_run = 1
    for iteration in range(int(max_passes)):
        if current.empty:
            break
        _overlap_details(
            path, current, pass_number=iteration, cleanup_actions=cleanup_actions,
            cleanup_area_km2=cleanup_area,
        )
        if (current["overlap_area_km2"] > float(max_artifact_km2)).any():
            worst = current.sort_values("overlap_area_km2", ascending=False).iloc[0]
            raise RuntimeError(
                f"Persisted {path.name} has a substantive overlap between "
                f"{worst.candidate_id_1}/{worst.candidate_id_2} "
                f"({worst.overlap_area_km2:.9f} km2 > {max_artifact_km2:.9f} km2); "
                f"details: {path.parent / (path.stem + '_topology_overlaps.csv')}"
            )
        updated, actions, area = _reconcile_persistence_artifacts(
            persisted, current, max_artifact_km2=float(max_artifact_km2),
            verify_tolerance_km2=float(verify_tolerance_km2),
        )
        if not actions:
            break
        cleanup_actions += actions
        cleanup_area += area
        updated = updated.set_index("candidate_id").loc[expected_ids].reset_index()
        if "exclusive_area_km2" in updated.columns:
            # A tiny post-write topology correction changes the serialized
            # geometry. Keep its cached area column consistent with the final
            # polygon rather than the earlier, pre-cleanup snapshot.
            updated["exclusive_area_km2"] = [geodesic_area_km2(g) for g in updated.geometry]
        updated.to_parquet(path, index=False)
        persisted = _read_persisted_parquet(path, expected_ids)
        current = pair_overlap_table(persisted, float(verify_tolerance_km2))
        checks_run += 1
    if not current.empty:
        _overlap_details(path, current, pass_number=checks_run, cleanup_actions=cleanup_actions,
                         cleanup_area_km2=cleanup_area)
        top = current.sort_values("overlap_area_km2", ascending=False).iloc[0]
        raise RuntimeError(
            f"Persisted topology has {len(current)} residual overlap pair(s) "
            f"in {path.name} after {checks_run} verification passes; "
            f"largest: {top.candidate_id_1}/{top.candidate_id_2} "
            f"{top.overlap_area_km2:.9f} km2; "
            f"details: {path.parent / (path.stem + '_topology_overlaps.csv')}"
        )
    return persisted, {
        "overlap_pairs_detected_after_write": initial_pairs,
        "overlap_area_km2_detected_after_write": initial_area,
        "cleanup_actions": int(cleanup_actions),
        "cleanup_area_km2": float(cleanup_area),
        "persistence_verification_passes": int(checks_run),
        "persisted_overlap_pairs_after": 0,
        "persisted_overlap_area_km2_after": 0.0,
        "postwrite_overlap_tolerance_km2": float(verify_tolerance_km2),
    }
