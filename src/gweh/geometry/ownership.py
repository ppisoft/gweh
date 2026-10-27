from __future__ import annotations

from typing import Any

import geopandas as gpd
import pandas as pd
from pyproj import CRS

from ..geography.common import project_geometry
from .common import eligibility_geometry


# Persist only the inter-stage scientific contract; full diagnostics are checked in memory.
OWNERSHIP_EXPORT_COLUMNS = [
    'phase_id', 'owner_candidate_id', 'project_surface_type',
    'owner_within_tolerance_of_evidence', 'ownership_method',
    'ownership_distance_km', 'ownership_used_tolerance',
    'source_surface_type', 'verified_physical_surface',
    'analytical_surface_type', 'surface_verification_method',
    'site_geometry_surface', 'owner_candidate_surface', 'unassigned_reason',
    'raw_installation_type', 'surface_correction_evidence_url',
]


def build_membership_admissibility(
    membership: pd.DataFrame,
    active: gpd.GeoDataFrame,
    candidates: gpd.GeoDataFrame,
) -> pd.DataFrame:
    """Active routed memberships that are surface-compatible with Stage 05 candidates."""
    candidate_surface = dict(
        zip(candidates["candidate_id"].astype(str), candidates["surface_type"].astype(str).str.lower())
    )
    frame = membership.loc[
        membership["phase_id"].astype(str).isin(set(active["phase_id"].astype(str)))
        & membership["is_active_boundary"].astype(bool)
        & membership["candidate_membership_effective"].astype(bool)
    ].copy()
    unknown = sorted(set(frame["candidate_id"].astype(str)) - set(candidate_surface))
    if unknown:
        raise RuntimeError(f"Membership contains candidates absent from Stage 05: {unknown}")
    frame["phase_id"] = frame["phase_id"].astype(str)
    frame["candidate_id"] = frame["candidate_id"].astype(str)
    # The Stage-04 routing field must never be a substitute for GWPT or an
    # APPROVED correction. Reject stale legacy override outputs at this gate.
    if frame['surface_resolution_method'].astype(str).eq('AUTO_GEOMETRY_OVERRIDE').any():
        raise RuntimeError('Legacy AUTO_GEOMETRY_OVERRIDE is not admissible')
    frame["project_surface_type"] = frame["project_surface_type_analytical"].astype(str).str.lower()
    if not frame['project_surface_type_effective'].astype(str).str.lower().eq(frame['project_surface_type']).all():
        raise RuntimeError('Routed membership physical surface differs from verified analytical type')
    frame["candidate_surface_type"] = frame["candidate_id"].map(candidate_surface)
    compatible = (
        frame["project_surface_type"].isin(['onshore', 'offshore'])
        & (frame["candidate_surface_type"].eq("mixed") | frame["project_surface_type"].eq(frame["candidate_surface_type"]))
    )
    return frame.loc[compatible, ["phase_id", "candidate_id", "project_surface_type"]].reset_index(drop=True)


def _spatial_hits(
    points,
    regions,
    tolerance_km,
):
    result = {str(phase_id): [] for phase_id in points["phase_id"].astype(str)}
    if points.empty or regions.empty:
        return result
    left = points[["phase_id", "geometry"]].copy()
    right = regions[["candidate_id", "geometry"]].copy()
    if float(tolerance_km) > 0:
        right["geometry"] = [eligibility_geometry(geometry, tolerance_km) for geometry in right.geometry]
        right = gpd.GeoDataFrame(right, geometry="geometry", crs=regions.crs)
    joined = gpd.sjoin(left, right, how="inner", predicate="intersects")
    for phase_id, group in joined.groupby("phase_id", sort=False):
        result[str(phase_id)] = sorted(group["candidate_id"].astype(str).unique().tolist())
    return result


def _point_local_crs(point):
    return CRS.from_proj4(
        f"+proj=aeqd +lat_0={float(point.y):.10f} +lon_0={float(point.x):.10f} "
        "+datum=WGS84 +units=m +no_defs"
    )


def _distance_to_geometry_km(point, geometry):
    local = _point_local_crs(point)
    projected_point = project_geometry(point, "EPSG:4326", local)
    projected_geometry = project_geometry(geometry, "EPSG:4326", local)
    return float(projected_point.distance(projected_geometry)) / 1000.0


def _nearest_candidate(point, candidate_ids, geometries):
    values = sorted(
        (float(_distance_to_geometry_km(point, geometries[candidate_id])), candidate_id)
        for candidate_id in candidate_ids
    )
    return values[0][1]


def assign_active_project_ownership(
    *,
    active: gpd.GeoDataFrame,
    membership: pd.DataFrame,
    evidence_base: gpd.GeoDataFrame,
    denominator_base: gpd.GeoDataFrame,
    evidence_exclusive: gpd.GeoDataFrame,
    denominator_exclusive: gpd.GeoDataFrame,
    position_tolerance_km: float,
    land_geometry=None,
    ocean_geometry=None,
) -> tuple[pd.DataFrame, pd.DataFrame, int]:
    """Assign each active project once, preferring strict spatial evidence."""
    active = active.to_crs("EPSG:4326").copy()
    active["phase_id"] = active["phase_id"].astype(str)
    if active["phase_id"].duplicated().any():
        raise RuntimeError("Active GWPT phase_id is not unique")

    admissible = build_membership_admissibility(membership, active, evidence_exclusive)
    by_phase = {
        str(phase_id): sorted(group["candidate_id"].astype(str).unique().tolist())
        for phase_id, group in admissible.groupby("phase_id", sort=False)
    }
    candidate_surface = dict(zip(
        evidence_exclusive['candidate_id'].astype(str),
        evidence_exclusive['surface_type'].astype(str).str.lower(),
    ))
    # Candidate membership explains why a phase has no compatible owner.
    claims_by_phase = {
        str(phase): group['surface_resolution_method'].astype(str).tolist()
        for phase, group in membership.loc[membership['is_active_boundary'].astype(bool)].groupby('phase_id', sort=False)
    }
    from shapely.prepared import prep
    land_prepared = prep(land_geometry) if land_geometry is not None else None
    ocean_prepared = prep(ocean_geometry) if ocean_geometry is not None else None

    tolerance_km = float(position_tolerance_km)
    strict_base_evidence_hits = _spatial_hits(active, evidence_base, 0.0)
    strict_base_denominator_hits = _spatial_hits(active, denominator_base, 0.0)
    strict_evidence_hits = _spatial_hits(active, evidence_exclusive, 0.0)
    strict_denominator_hits = _spatial_hits(active, denominator_exclusive, 0.0)

    if tolerance_km > 0.0:
        tolerant_base_evidence_hits = _spatial_hits(active, evidence_base, tolerance_km)
        tolerant_base_denominator_hits = _spatial_hits(active, denominator_base, tolerance_km)
        tolerant_evidence_hits = _spatial_hits(active, evidence_exclusive, tolerance_km)
        tolerant_denominator_hits = _spatial_hits(active, denominator_exclusive, tolerance_km)
    else:
        tolerant_base_evidence_hits = strict_base_evidence_hits
        tolerant_base_denominator_hits = strict_base_denominator_hits
        tolerant_evidence_hits = strict_evidence_hits
        tolerant_denominator_hits = strict_denominator_hits

    evidence_geometry = {
        str(row.candidate_id): row.geometry for row in evidence_exclusive.itertuples(index=False)
    }
    denominator_geometry = {
        str(row.candidate_id): row.geometry for row in denominator_exclusive.itertuples(index=False)
    }
    base_geometry = {
        str(row.candidate_id): row.geometry for row in evidence_base.itertuples(index=False)
    }

    rows = []
    spatial_exclusion_count = 0
    for row in active.itertuples(index=False):
        phase_id = str(row.phase_id)
        candidates = by_phase.get(phase_id, [])
        candidate_set = set(candidates)

        base_evidence_strict = sorted(candidate_set & set(strict_base_evidence_hits.get(phase_id, [])))
        base_denominator_strict = sorted(candidate_set & set(strict_base_denominator_hits.get(phase_id, [])))
        base_strict = sorted(set(base_evidence_strict) | set(base_denominator_strict))

        base_evidence_tolerant = sorted(candidate_set & set(tolerant_base_evidence_hits.get(phase_id, [])))
        base_denominator_tolerant = sorted(candidate_set & set(tolerant_base_denominator_hits.get(phase_id, [])))
        base_tolerant = sorted(set(base_evidence_tolerant) | set(base_denominator_tolerant))

        spatial_candidates = base_strict if base_strict else base_tolerant
        spatial_basis = "STRICT_PREPARTITION" if base_strict else ("TOLERANT_PREPARTITION" if base_tolerant else "NONE")

        spatial_exclusion_count += len(candidate_set - set(spatial_candidates))

        eligible = set(spatial_candidates)
        evidence_strict = sorted(eligible & set(strict_evidence_hits.get(phase_id, [])))
        denominator_strict = sorted(eligible & set(strict_denominator_hits.get(phase_id, [])))
        evidence_tolerant = sorted(eligible & set(tolerant_evidence_hits.get(phase_id, [])))
        denominator_tolerant = sorted(eligible & set(tolerant_denominator_hits.get(phase_id, [])))

        owner = ""
        method = "NO_SPATIALLY_ELIGIBLE_MEMBERSHIP"
        distance_km = float("nan")
        priority = (
            (evidence_strict, evidence_geometry, "STRICT_UNIQUE_EVIDENCE_HIT", "STRICT_EVIDENCE_BOUNDARY_TIE_GEOMETRY", True),
            (denominator_strict, denominator_geometry, "STRICT_UNIQUE_DENOMINATOR_HIT", "STRICT_DENOMINATOR_BOUNDARY_TIE_GEOMETRY", True),
            (evidence_tolerant, evidence_geometry, "TOLERANCE_UNIQUE_EVIDENCE_HIT", "TOLERANCE_EVIDENCE_TIE_NEAREST_GEOMETRY", False),
            (denominator_tolerant, denominator_geometry, "TOLERANCE_UNIQUE_DENOMINATOR_HIT", "TOLERANCE_DENOMINATOR_TIE_NEAREST_GEOMETRY", False),
        )
        for hits, geometries, unique_method, tie_method, exact_unique in priority:
            if not hits:
                continue
            if len(hits) == 1:
                owner, method = hits[0], unique_method
                distance_km = 0.0 if exact_unique else _distance_to_geometry_km(row.geometry, geometries[owner])
            else:
                owner = _nearest_candidate(row.geometry, hits, geometries)
                method = tie_method
                distance_km = _distance_to_geometry_km(row.geometry, geometries[owner])
            break
        else:
            if len(spatial_candidates) == 1:
                owner, method = spatial_candidates[0], "SINGLE_SPATIALLY_ELIGIBLE_MEMBERSHIP"
                distance_km = _distance_to_geometry_km(row.geometry, base_geometry[owner])
            elif len(spatial_candidates) > 1:
                owner = _nearest_candidate(row.geometry, spatial_candidates, base_geometry)
                method = "SPATIALLY_ELIGIBLE_NEAREST_GEOMETRY"
                distance_km = _distance_to_geometry_km(row.geometry, base_geometry[owner])

        source_surface = str(getattr(row, 'surface_type_source', 'unknown')).lower()
        verified_physical = str(getattr(row, 'surface_type_verified', source_surface)).lower()
        analytical = str(getattr(row, 'surface_type_analytical', source_surface)).lower()
        verification_method = str(getattr(row, 'surface_verification_method', 'SOURCE_UNCORRECTED'))
        # This remains the analytical component in the Stage-06 market contract.
        project_surface = analytical
        if owner:
            if (analytical not in {'onshore','offshore'}
                    or candidate_surface.get(owner) not in {analytical, 'mixed'}):
                raise RuntimeError(f'{phase_id}: incompatible source/verified surface ownership {owner}')
            unassigned_reason = ''
        elif analytical == 'unknown':
            unassigned_reason = 'UNASSIGNED_UNKNOWN_SURFACE'
        else:
            methods = claims_by_phase.get(phase_id, [])
            if any(method.startswith('SURFACE_CONFLICT') for method in methods):
                unassigned_reason = 'UNASSIGNED_SURFACE_CONFLICT'
            elif not methods:
                unassigned_reason = 'UNASSIGNED_NO_CANDIDATE_CLAIM'
            elif candidates and not spatial_candidates:
                unassigned_reason = 'UNASSIGNED_NO_SPATIALLY_ELIGIBLE_OWNER'
            elif any(method == 'UNROUTED_OUTSIDE_TOLERANCE' for method in methods):
                unassigned_reason = 'UNASSIGNED_OUTSIDE_TOLERANCE'
            else:
                unassigned_reason = 'UNASSIGNED_NO_COMPATIBLE_OWNER'
        # A point-overlay indicator is diagnostics only: a GWPT coordinate
        # can be a project centroid or administrative placeholder.
        if verified_physical == 'inland_water':
            site_geometry_surface = 'INLAND_WATER_FROM_VERIFIED_EVIDENCE'
        elif land_prepared is None or ocean_prepared is None:
            site_geometry_surface = 'NOT_EVALUATED'
        else:
            in_land = bool(land_prepared.intersects(row.geometry))
            in_ocean = bool(ocean_prepared.intersects(row.geometry))
            site_geometry_surface = (
                'AMBIGUOUS_LAND_MARINE_MASK' if in_land and in_ocean else
                'LAND_MASK' if in_land else 'MARINE_OCEAN_MASK' if in_ocean else
                'OUTSIDE_LAND_MARINE_MASK'
            )
        rows.append(
            {
                "phase_id": phase_id,
                "owner_candidate_id": owner,
                "n_admissible_candidates": len(candidates),
                "admissible_candidate_ids": ";".join(candidates),
                "n_spatially_eligible_candidates": len(spatial_candidates),
                "spatially_eligible_candidate_ids": ";".join(spatial_candidates),
                "spatial_eligibility_basis": spatial_basis,
                "n_base_evidence_hits_admissible": len(base_evidence_strict),
                "n_base_denominator_hits_admissible": len(base_denominator_strict),
                "n_evidence_hits_admissible": len(evidence_strict),
                "n_denominator_hits_admissible": len(denominator_strict),
                "ownership_method": method,
                "ownership_distance_km": distance_km,
                "ownership_used_tolerance": bool("TOLERANCE_" in method or spatial_basis == "TOLERANT_PREPARTITION"),
                "owner_inside_evidence": bool(owner and owner in evidence_strict),
                "owner_inside_denominator": bool(owner and owner in denominator_strict),
                "owner_inside_prepartition_evidence": bool(owner and owner in base_evidence_strict),
                "owner_inside_prepartition_denominator": bool(owner and owner in base_denominator_strict),
                "owner_within_tolerance_of_evidence": bool(owner and owner in evidence_tolerant),
                "owner_within_tolerance_of_denominator": bool(owner and owner in denominator_tolerant),
                "owner_within_tolerance_of_prepartition_evidence": bool(owner and owner in base_evidence_tolerant),
                "owner_within_tolerance_of_prepartition_denominator": bool(owner and owner in base_denominator_tolerant),
                "capacity_mw": float(getattr(row, "capacity_mw", 0.0)),
                "status": str(getattr(row, "status", "")),
                "project_surface_type": project_surface,
                "source_surface_type": source_surface,
                "verified_physical_surface": verified_physical,
                "analytical_surface_type": analytical,
                "surface_verification_method": verification_method,
                "site_geometry_surface": site_geometry_surface,
                "owner_candidate_surface": candidate_surface.get(owner, '') if owner else '',
                "unassigned_reason": unassigned_reason,
                "country_area": str(getattr(row, "country_area", "")),
                "raw_installation_type": str(getattr(row, "installation_type_source", "")),
                "surface_correction_evidence_url": str(getattr(row, "surface_correction_evidence_url", "")),
            }
        )

    return pd.DataFrame(rows), admissible, spatial_exclusion_count

def ownership_invariants(
    ownership: pd.DataFrame,
    admissible: pd.DataFrame,
    candidates: gpd.GeoDataFrame,
) -> dict[str, Any]:
    owned = ownership["owner_candidate_id"].astype(str).str.len().gt(0)
    owned_rows = ownership.loc[owned].copy()
    admissible_phase_ids = set(admissible["phase_id"].astype(str))
    admissible_unowned = (~owned) & ownership["phase_id"].astype(str).isin(admissible_phase_ids)
    admissible_unowned_rows = ownership.loc[admissible_unowned].copy()
    admissible_pairs = set(zip(admissible["phase_id"].astype(str), admissible["candidate_id"].astype(str)))
    owner_pairs = list(zip(owned_rows["phase_id"].astype(str), owned_rows["owner_candidate_id"].astype(str)))
    owner_not_admissible = [pair for pair in owner_pairs if pair not in admissible_pairs]

    spatial_pairs = set()
    for row in ownership.itertuples(index=False):
        for candidate_id in [item for item in str(row.spatially_eligible_candidate_ids).split(";") if item]:
            spatial_pairs.add((str(row.phase_id), candidate_id))
    owner_not_spatial = [pair for pair in owner_pairs if pair not in spatial_pairs]

    candidate_surface = dict(zip(candidates["candidate_id"].astype(str), candidates["surface_type"].astype(str).str.lower()))
    surface_mismatch = []
    for row in owned_rows.itertuples(index=False):
        surface = candidate_surface.get(str(row.owner_candidate_id), "")
        project_surface = str(row.project_surface_type).lower()
        if surface and surface != "mixed" and project_surface != surface:
            surface_mismatch.append((str(row.phase_id), str(row.owner_candidate_id)))

    return {
        "owned_active_rows": int(owned.sum()),
        "owned_active_mw": float(pd.to_numeric(owned_rows.get("capacity_mw", 0), errors="coerce").fillna(0.0).sum()),
        "admissible_unowned_active_rows": int(admissible_unowned.sum()),
        "admissible_unowned_active_mw": float(
            pd.to_numeric(admissible_unowned_rows.get("capacity_mw", 0), errors="coerce").fillna(0.0).sum()
        ),
        "owner_not_admissible_count": len(owner_not_admissible),
        "owner_not_spatially_eligible_count": len(owner_not_spatial),
        "surface_mismatch_owned_count": len(surface_mismatch),
        "boundary_ties_resolved": int(ownership["ownership_method"].astype(str).str.contains("TIE", regex=False).sum()),
        "tolerance_resolved_rows": int(ownership["ownership_used_tolerance"].astype(bool).sum()),
        "strict_evidence_owned_rows": int(ownership["ownership_method"].astype(str).str.startswith("STRICT_").sum()),
        "nearest_fallback_rows": int(ownership["ownership_method"].astype(str).eq("SPATIALLY_ELIGIBLE_NEAREST_GEOMETRY").sum()),
    }
